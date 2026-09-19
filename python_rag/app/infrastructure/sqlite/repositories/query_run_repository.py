# -*- coding: utf-8 -*-
"""查询运行仓储的 SQLite 实现：状态机迁移、幂等创建与评测事实回填

状态机：queued → running → completed/failed/cancelled，cancel_requested
为取消中过渡态（执行方检查点收尾）；终态迁移回填完成时间与总耗时。
重启恢复把残留非终态查询统一置为 failed（进程重启中断）。
"""
from app.domain.clock import utc_now_iso
from app.domain.entities import QueryRun, QueryRunState
from app.domain.errors import EntityNotFoundError, QueryStateConflictError
from app.domain.ids import uuid7
from app.domain.ports import QueryRunRepository as QueryRunRepositoryPort

from ..transactions import run_in_transaction

# 终态集合：进入终态后不再迁移
_TERMINAL_STATES = {
    QueryRunState.COMPLETED.value,
    QueryRunState.FAILED.value,
    QueryRunState.CANCELLED.value,
}

_RUN_COLUMNS = (
    "id, knowledge_base_id, question, state, conversation_id, user_message_id,"
    " assistant_message_id, refused, rerank_degraded, started_at, first_token_at,"
    " completed_at, server_ttft_ms, total_ms, error_code, error_message,"
    " idempotency_key, created_at"
)


class SQLiteQueryRunRepository(QueryRunRepositoryPort):
    """query_runs 表的仓储实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def create(
        self,
        *,
        kb_id: str,
        question: str,
        config_ids: dict[str, str],
        idempotency_key: str | None = None,
    ) -> QueryRun:
        def _create(conn) -> QueryRun:
            if idempotency_key is not None:
                existing = conn.execute(
                    "SELECT id FROM query_runs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._load(conn, existing[0])

            exists = conn.execute(
                "SELECT 1 FROM knowledge_bases WHERE id = ?", (kb_id,)
            ).fetchone()
            if exists is None:
                raise EntityNotFoundError(f"知识库不存在: {kb_id}")

            run_id = uuid7()
            created_at = utc_now_iso()
            conn.execute(
                "INSERT INTO query_runs ("
                " id, knowledge_base_id, question, state, retrieval_config_id,"
                " rerank_config_id, context_config_id, generation_config_id,"
                " idempotency_key, created_at)"
                " VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    kb_id,
                    question,
                    config_ids.get("retrieval"),
                    config_ids.get("rerank"),
                    config_ids.get("context"),
                    config_ids.get("generation"),
                    idempotency_key,
                    created_at,
                ),
            )
            return self._load(conn, run_id)

        return run_in_transaction(self._conn, _create, "创建查询运行")

    def get(self, run_id: str) -> QueryRun | None:
        row = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM query_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return self._to_entity(row) if row is not None else None

    def mark_running(self, run_id: str) -> None:
        def _mark(conn) -> None:
            self._require(conn, run_id)
            cursor = conn.execute(
                "UPDATE query_runs SET state = 'running', started_at = ?"
                " WHERE id = ? AND state = 'queued'",
                (utc_now_iso(), run_id),
            )
            if cursor.rowcount == 0:
                raise QueryStateConflictError(
                    f"查询 {run_id} 状态不允许进入 running"
                )

        run_in_transaction(self._conn, _mark, f"查询进入执行 {run_id}")

    def mark_first_token(self, run_id: str) -> None:
        def _mark(conn) -> None:
            self._require(conn, run_id)
            now = utc_now_iso()
            conn.execute(
                "UPDATE query_runs SET first_token_at = ?,"
                " server_ttft_ms = CAST("
                "  (julianday(?) - julianday(started_at)) * 86400000 AS INTEGER)"
                " WHERE id = ? AND first_token_at IS NULL AND started_at IS NOT NULL",
                (now, now, run_id),
            )

        run_in_transaction(self._conn, _mark, f"记录查询首 token {run_id}")

    def request_cancel(self, run_id: str) -> QueryRun:
        def _request(conn) -> QueryRun:
            row = self._get_row(conn, run_id)
            if row is None:
                raise EntityNotFoundError(f"查询不存在: {run_id}")
            state = row[3]
            if state in ("queued", "running"):
                conn.execute(
                    "UPDATE query_runs SET state = 'cancel_requested' WHERE id = ?",
                    (run_id,),
                )
            return self._load(conn, run_id)

        return run_in_transaction(self._conn, _request, f"请求取消查询 {run_id}")

    def is_cancel_requested(self, run_id: str) -> bool:
        row = self._conn.execute(
            "SELECT state FROM query_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row is not None and row[0] == QueryRunState.CANCEL_REQUESTED.value

    def finalize(
        self,
        run_id: str,
        *,
        state: str,
        refused: bool = False,
        degraded: bool = False,
        assistant_message_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> QueryRun:
        if state not in _TERMINAL_STATES:
            raise QueryStateConflictError(f"非法终态: {state}")

        def _finalize(conn) -> QueryRun:
            row = self._get_row(conn, run_id)
            if row is None:
                raise EntityNotFoundError(f"查询不存在: {run_id}")
            current = row[3]
            if current in _TERMINAL_STATES:
                raise QueryStateConflictError(
                    f"查询 {run_id} 已处于终态 {current}"
                )
            now = utc_now_iso()
            conn.execute(
                "UPDATE query_runs SET state = ?, completed_at = ?,"
                " total_ms = CAST("
                "  (julianday(?) - julianday(created_at)) * 86400000 AS INTEGER),"
                " refused = ?, rerank_degraded = ?, assistant_message_id = ?,"
                " error_code = ?, error_message = ?"
                " WHERE id = ?",
                (
                    state, now, now,
                    1 if refused else 0,
                    1 if degraded else 0,
                    assistant_message_id,
                    error_code, error_message, run_id,
                ),
            )
            return self._load(conn, run_id)

        return run_in_transaction(self._conn, _finalize, f"查询收尾 {run_id}")

    def attach_message_ids(
        self,
        run_id: str,
        *,
        conversation_id: str,
        user_message_id: str,
        assistant_message_id: str,
    ) -> None:
        """回填查询与会话/消息的关联（方法契约见领域 Port 定义）"""

        def _attach(conn) -> None:
            self._require(conn, run_id)
            conn.execute(
                "UPDATE query_runs SET conversation_id = ?,"
                " user_message_id = ?, assistant_message_id = ? WHERE id = ?",
                (conversation_id, user_message_id, assistant_message_id, run_id),
            )

        run_in_transaction(self._conn, _attach, f"回填查询消息关联 {run_id}")

    def record_segments(
        self,
        run_id: str,
        *,
        retrieval_ms: int,
        rerank_ms: int,
        prompt_build_ms: int,
        model_ttft_ms: int,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        """记录分段耗时与生成用量（方法契约见领域 Port 定义）"""

        def _record(conn) -> None:
            self._require(conn, run_id)
            conn.execute(
                "UPDATE query_runs SET retrieval_ms = ?, rerank_ms = ?,"
                " prompt_build_ms = ?, model_ttft_ms = ?, input_tokens = ?,"
                " output_tokens = ? WHERE id = ?",
                (
                    retrieval_ms, rerank_ms, prompt_build_ms, model_ttft_ms,
                    input_tokens, output_tokens, run_id,
                ),
            )

        run_in_transaction(self._conn, _record, f"记录查询分段指标 {run_id}")

    def record_candidates(self, run_id: str, records) -> int:
        """批量写入候选快照（方法契约见领域 Port 定义）"""

        def _record(conn) -> int:
            self._require(conn, run_id)
            written = 0
            for record in records:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO retrieval_candidates ("
                    " id, query_run_id, chunk_id, source, vector_rank,"
                    " vector_score, keyword_rank, keyword_score, rrf_rank,"
                    " rrf_score, rerank_rank, rerank_score, in_context, created_at)"
                    f" VALUES ({', '.join(['?'] * 14)})",
                    (
                        uuid7(), run_id, record.chunk_id, record.source,
                        record.vector_rank, record.vector_score,
                        record.keyword_rank, record.keyword_score,
                        record.rrf_rank, record.rrf_score,
                        record.rerank_rank, record.rerank_score,
                        1 if record.in_context else 0, utc_now_iso(),
                    ),
                )
                written += cursor.rowcount
            return written

        return run_in_transaction(
            self._conn, _record, f"写入查询候选 {run_id}"
        )

    def record_client_metric(
        self,
        run_id: str,
        *,
        client_send_at: str,
        first_sse_token_received_at: str,
        first_token_rendered_at: str,
        client_ttft_ms: int,
        client_instance_id_hash: str | None,
        network_context_json: str | None,
    ) -> bool:
        """记录客户端遥测（方法契约见领域 Port 定义）"""

        def _record(conn) -> bool:
            self._require(conn, run_id)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO query_client_metrics ("
                " query_run_id, client_send_at, first_sse_token_received_at,"
                " first_token_rendered_at, client_ttft_ms,"
                " client_instance_id_hash, network_context_json, reported_at)"
                f" VALUES ({', '.join(['?'] * 8)})",
                (
                    run_id, client_send_at, first_sse_token_received_at,
                    first_token_rendered_at, client_ttft_ms,
                    client_instance_id_hash, network_context_json,
                    utc_now_iso(),
                ),
            )
            return cursor.rowcount > 0

        return run_in_transaction(
            self._conn, _record, f"记录客户端遥测 {run_id}"
        )

    def fail_interrupted(self) -> int:
        def _fail(conn) -> int:
            now = utc_now_iso()
            cursor = conn.execute(
                "UPDATE query_runs SET state = 'failed', completed_at = ?,"
                " total_ms = CAST("
                "  (julianday(?) - julianday(created_at)) * 86400000 AS INTEGER),"
                " error_code = 'INTERNAL_ERROR',"
                " error_message = '服务重启导致查询中断'"
                " WHERE state IN ('queued', 'running', 'cancel_requested')",
                (now, now),
            )
            return cursor.rowcount

        return run_in_transaction(self._conn, _fail, "恢复中断查询")

    def _load(self, conn, run_id: str) -> QueryRun:
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM query_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"查询不存在: {run_id}")
        return self._to_entity(row)

    def _require(self, conn, run_id: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM query_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if exists is None:
            raise EntityNotFoundError(f"查询不存在: {run_id}")

    def _get_row(self, conn, run_id: str):
        return conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM query_runs WHERE id = ?", (run_id,)
        ).fetchone()

    @staticmethod
    def _to_entity(row) -> QueryRun:
        return QueryRun(
            id=row[0],
            knowledge_base_id=row[1],
            question=row[2],
            state=QueryRunState(row[3]),
            conversation_id=row[4],
            user_message_id=row[5],
            assistant_message_id=row[6],
            refused=bool(row[7]),
            rerank_degraded=bool(row[8]),
            started_at=row[9],
            first_token_at=row[10],
            completed_at=row[11],
            server_ttft_ms=row[12],
            total_ms=row[13],
            error_code=row[14],
            error_message=row[15],
            idempotency_key=row[16],
            created_at=row[17],
        )
