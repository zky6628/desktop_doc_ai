# -*- coding: utf-8 -*-
"""外部任务仓储的 SQLite 实现：云端批次关联与轮询事实

以 (provider, provider_batch_ref, source_ref) 为稳定唯一键做幂等
登记：同键重放返回既有记录，恢复流程据此定位既有批次而不重复
提交。轮询记录按累加方式写入计数，避免调用方并发覆盖。
"""
from app.domain.clock import utc_now_iso
from app.domain.entities import ExternalTask
from app.domain.errors import EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.ports import ExternalTaskRepository as ExternalTaskRepositoryPort

from ..transactions import run_in_transaction

# 外部任务列的读取顺序（与 _to_entity 一一对应）
_EXTERNAL_TASK_COLUMNS = (
    "id, task_id, provider, provider_batch_ref, source_ref, provider_task_id,"
    " upload_url_expires_at, remote_cancel_state, provider_status_summary,"
    " state, poll_count, last_polled_at, request_summary_json,"
    " result_uri, result_sha256, expires_at"
)


# 登记时的初始供应方侧状态：批次已提交、尚未轮询
_INITIAL_STATE_SUBMITTED = "submitted"


def _to_entity(row) -> ExternalTask:
    """把查询行转换为领域实体"""
    return ExternalTask(
        id=row[0],
        task_id=row[1],
        provider=row[2],
        provider_batch_ref=row[3],
        source_ref=row[4],
        provider_task_id=row[5],
        upload_url_expires_at=row[6],
        remote_cancel_state=row[7],
        provider_status_summary=row[8],
        state=row[9],
        poll_count=row[10],
        last_polled_at=row[11],
        request_summary_json=row[12],
        result_uri=row[13],
        result_sha256=row[14],
        expires_at=row[15],
    )


class SQLiteExternalTaskRepository(ExternalTaskRepositoryPort):
    """external_tasks 表的仓储实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def register(
        self,
        *,
        task_id: str,
        provider: str,
        provider_batch_ref: str,
        source_ref: str,
        upload_url_expires_at: str | None = None,
        request_summary_json: str | None = None,
    ) -> ExternalTask:
        """幂等登记一个源文件的云端关联（方法契约见领域 Port 定义）"""

        def _register(conn) -> ExternalTask:
            existing = self._get_by_refs_row(
                conn, provider, provider_batch_ref, source_ref
            )
            if existing is not None:
                return _to_entity(existing)
            external_id = uuid7()
            conn.execute(
                "INSERT INTO external_tasks"
                " (id, task_id, provider, provider_batch_ref, source_ref,"
                "  provider_task_id, upload_url_expires_at, remote_cancel_state,"
                "  provider_status_summary, state, poll_count, last_polled_at,"
                "  request_summary_json, result_uri, result_sha256, expires_at)"
                " VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, 0, NULL,"
                "  ?, NULL, NULL, NULL)",
                (
                    external_id, task_id, provider, provider_batch_ref,
                    source_ref, upload_url_expires_at,
                    _INITIAL_STATE_SUBMITTED, request_summary_json,
                ),
            )
            return _to_entity(self._get_row(conn, external_id))

        return run_in_transaction(
            self._conn,
            _register,
            f"登记外部任务 {provider}/{provider_batch_ref}/{source_ref}",
        )

    def get_by_refs(
        self, provider: str, provider_batch_ref: str, source_ref: str
    ) -> ExternalTask | None:
        row = self._get_by_refs_row(
            self._conn, provider, provider_batch_ref, source_ref
        )
        return _to_entity(row) if row is not None else None

    def list_by_task(self, task_id: str) -> list[ExternalTask]:
        # rowid 随插入单调递增，按其排序即登记顺序
        rows = self._conn.execute(
            f"SELECT {_EXTERNAL_TASK_COLUMNS} FROM external_tasks"
            " WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        ).fetchall()
        return [_to_entity(row) for row in rows]

    def record_poll(
        self,
        external_task_id: str,
        *,
        state: str,
        status_summary: str | None = None,
        provider_task_id: str | None = None,
    ) -> ExternalTask:
        """累加轮询计数并刷新状态快照（方法契约见领域 Port 定义）"""

        def _record(conn) -> ExternalTask:
            row = self._get_row(conn, external_task_id)
            if row is None:
                raise EntityNotFoundError(f"外部任务不存在: {external_task_id}")
            now = utc_now_iso()
            conn.execute(
                "UPDATE external_tasks SET"
                " poll_count = poll_count + 1, last_polled_at = ?,"
                " state = ?, provider_status_summary = ?,"
                " provider_task_id = COALESCE(?, provider_task_id)"
                " WHERE id = ?",
                (now, state, status_summary, provider_task_id, external_task_id),
            )
            return _to_entity(self._get_row(conn, external_task_id))

        return run_in_transaction(
            self._conn, _record, f"记录外部任务轮询 {external_task_id}"
        )

    def set_result(
        self,
        external_task_id: str,
        *,
        result_sha256: str,
        expires_at: str | None = None,
        state: str | None = None,
    ) -> ExternalTask:
        """记录结果事实（方法契约见领域 Port 定义）"""

        def _set_result(conn) -> ExternalTask:
            row = self._get_row(conn, external_task_id)
            if row is None:
                raise EntityNotFoundError(f"外部任务不存在: {external_task_id}")
            conn.execute(
                "UPDATE external_tasks SET result_sha256 = ?, expires_at = ?,"
                " state = COALESCE(?, state) WHERE id = ?",
                (result_sha256, expires_at, state, external_task_id),
            )
            return _to_entity(self._get_row(conn, external_task_id))

        return run_in_transaction(
            self._conn, _set_result, f"记录外部任务结果 {external_task_id}"
        )

    @staticmethod
    def _get_row(conn, external_task_id: str):
        """按 ID 读取原始行"""
        return conn.execute(
            f"SELECT {_EXTERNAL_TASK_COLUMNS} FROM external_tasks WHERE id = ?",
            (external_task_id,),
        ).fetchone()

    @staticmethod
    def _get_by_refs_row(conn, provider: str, provider_batch_ref: str, source_ref: str):
        """按稳定唯一键读取原始行"""
        return conn.execute(
            f"SELECT {_EXTERNAL_TASK_COLUMNS} FROM external_tasks"
            " WHERE provider = ? AND provider_batch_ref = ? AND source_ref = ?",
            (provider, provider_batch_ref, source_ref),
        ).fetchone()
