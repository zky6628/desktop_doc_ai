# -*- coding: utf-8 -*-
"""导入仓储的 SQLite 实现：单事务建立文档、版本与导入任务

导入是批量的第一步：暂存文件 facts（路径/哈希/大小/MIME）到达时，
一次事务内完成知识库校验、重复内容判定、文档与版本建立和导入任务
创建。任务队列容量检查复用任务仓储的共享实现，保证两个入口对
3/50/53 口径的判定完全一致；事务中途任一步失败整体回滚，不留下
文档或版本残留。
"""
from app.domain.clock import utc_now_iso
from app.domain.entities import DocumentStatus, Task, TaskStage, TaskStatus
from app.domain.errors import (
    DuplicateActiveContentError,
    EntityNotFoundError,
)
from app.domain.ids import uuid7
from app.domain.ingest import DuplicatePolicy, ImportOutcome
from app.domain.ports import ImportRepository as ImportRepositoryPort

from ..transactions import run_in_transaction
from .task_repository import (
    ensure_queue_capacity,
    insert_task_event,
)

# 文档版本在导入时的初始状态：源文件已落定、解析尚未发生；
# 解析完成后由解析环节更新状态与解析内容哈希
_VERSION_STATUS_PENDING = "pending"

# 审计事件类型：与任务仓储共用同一套取值
_EVENT_CREATED = "created"

# 幂等键命中时需要回读的任务列
_TASK_COLUMNS = (
    "id, task_type, knowledge_base_id, document_id, document_version_id,"
    " index_version_id, state, stage, progress, priority, idempotency_key,"
    " retry_count, max_retries, attempt_count, stage_attempt, total_attempt_count,"
    " next_retry_at, lease_owner, lease_expires_at, heartbeat_at,"
    " cancel_requested_at, checkpoint_json, parent_task_id, retry_origin,"
    " error_code, error_message, input_json, created_at, started_at, finished_at"
)


def _task_from_row(row) -> Task:
    """把任务查询行转换为领域实体"""
    return Task(
        id=row[0],
        task_type=row[1],
        knowledge_base_id=row[2],
        document_id=row[3],
        document_version_id=row[4],
        index_version_id=row[5],
        state=TaskStatus(row[6]),
        stage=TaskStage(row[7]) if row[7] is not None else None,
        progress=row[8],
        priority=row[9],
        idempotency_key=row[10],
        retry_count=row[11],
        max_retries=row[12],
        attempt_count=row[13],
        stage_attempt=row[14],
        total_attempt_count=row[15],
        next_retry_at=row[16],
        lease_owner=row[17],
        lease_expires_at=row[18],
        heartbeat_at=row[19],
        cancel_requested_at=row[20],
        checkpoint_json=row[21],
        parent_task_id=row[22],
        retry_origin=row[23],
        error_code=row[24],
        error_message=row[25],
        input_json=row[26],
        created_at=row[27],
        started_at=row[28],
        finished_at=row[29],
    )


class SQLiteImportRepository(ImportRepositoryPort):
    """documents / document_versions / tasks 的导入组合写入

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def create_import(
        self,
        kb_id: str,
        *,
        display_name: str,
        source_path: str,
        source_sha256: str,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        duplicate_policy: str = "skip",
        task_type: str = "import",
        parser_mode: str | None = None,
        parser_route_json: str | None = None,
        idempotency_key: str | None = None,
    ) -> ImportOutcome:
        """单事务完成一次导入（方法契约见领域 Port 定义）"""

        def _import(conn) -> ImportOutcome:
            now = utc_now_iso()

            kb_exists = conn.execute(
                "SELECT 1 FROM knowledge_bases WHERE id = ?", (kb_id,)
            ).fetchone()
            if kb_exists is None:
                raise EntityNotFoundError(f"知识库不存在: {kb_id}")

            # 幂等重放优先于一切写入：同键命中直接返回既有任务，
            # 保证重放不会产生重复的文档与版本
            if idempotency_key is not None:
                existing = conn.execute(
                    f"SELECT {_TASK_COLUMNS} FROM tasks WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return ImportOutcome(
                        document_id=existing[3],
                        document_version_id=existing[4],
                        task=_task_from_row(existing),
                        reused_existing_document=True,
                    )

            # 重复内容判定：仅针对活动文档；命中后按策略拒绝或复用
            duplicate = conn.execute(
                "SELECT id FROM documents"
                " WHERE knowledge_base_id = ? AND source_sha256 = ?"
                " AND deleted_at IS NULL",
                (kb_id, source_sha256),
            ).fetchone()
            if duplicate is not None:
                if DuplicatePolicy(duplicate_policy) is DuplicatePolicy.SKIP:
                    raise DuplicateActiveContentError(
                        "知识库内已存在相同内容的活动文档"
                    )
                document_id = duplicate[0]
                reused = True
            else:
                document_id = uuid7()
                conn.execute(
                    "INSERT INTO documents"
                    " (id, knowledge_base_id, display_name, source_sha256, status,"
                    "  active_document_version_id, deleted_at, delete_requested_at,"
                    "  created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                    (
                        document_id, kb_id, display_name, source_sha256,
                        DocumentStatus.QUEUED.value, now, now,
                    ),
                )
                reused = False

            # 版本号在文档内递增：写事务串行化保证并发安全
            next_no = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM document_versions"
                " WHERE document_id = ?",
                (document_id,),
            ).fetchone()[0]
            version_id = uuid7()
            conn.execute(
                "INSERT INTO document_versions"
                " (id, document_id, version_no, source_path, source_sha256, mime_type,"
                "  size_bytes, parser_mode, parser_provider, parser_version,"
                "  parsed_content_sha256, status, active_index_version_id, created_at,"
                "  activated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, NULL)",
                (
                    version_id, document_id, next_no, source_path, source_sha256,
                    mime_type, size_bytes, parser_mode,
                    _VERSION_STATUS_PENDING, now,
                ),
            )

            # 容量检查放在版本插入之后、任务插入之前：任一口径超限
            # 即抛错回滚，文档与版本随之一并消失，满足"队列满不产生
            # 版本残留"的导入约束
            ensure_queue_capacity(conn)

            task_id = uuid7()
            conn.execute(
                "INSERT INTO tasks"
                " (id, task_type, knowledge_base_id, document_id, document_version_id,"
                "  index_version_id, state, stage, progress, priority, idempotency_key,"
                "  retry_count, max_retries, attempt_count, stage_attempt,"
                "  total_attempt_count, next_retry_at, lease_owner, lease_expires_at,"
                "  heartbeat_at, cancel_requested_at, checkpoint_json, parent_task_id,"
                "  retry_origin, error_code, error_message, input_json,"
                "  created_at, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, 0, 0, ?, 0, 3, 0, 0, 0,"
                "  NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?,"
                "  ?, NULL, NULL)",
                (
                    task_id, task_type, kb_id, document_id, version_id,
                    TaskStatus.QUEUED.value, idempotency_key,
                    parser_route_json, now,
                ),
            )
            insert_task_event(
                conn,
                task_id=task_id,
                event_type=_EVENT_CREATED,
                state=TaskStatus.QUEUED,
                stage=None,
                attempt_count=0,
                worker=None,
                created_at=now,
            )
            task_row = conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return ImportOutcome(
                document_id=document_id,
                document_version_id=version_id,
                task=_task_from_row(task_row),
                reused_existing_document=reused,
            )

        return run_in_transaction(
            self._conn,
            _import,
            f"导入文件 {display_name} 到知识库 {kb_id}",
        )
