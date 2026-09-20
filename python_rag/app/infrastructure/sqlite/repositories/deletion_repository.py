# -*- coding: utf-8 -*-
"""删除仓储的 SQLite 实现：软删除与删除任务的组合事务写入

删除与导入同构：一次事务内完成存在性校验、在途守卫、队列容量
检查、软删除时间写入与删除任务创建。容量口径复用任务仓储的共享
实现，保证与导入入口的 3/50/53 判定完全一致；事务中途任一步失败
整体回滚，资源保持未删除。
"""
from app.domain.clock import utc_now_iso
from app.domain.entities import (
    DocumentStatus,
    KnowledgeBaseStatus,
    Task,
    TaskStatus,
)
from app.domain.errors import (
    EntityNotFoundError,
    TaskStateConflictError,
)
from app.domain.ids import uuid7
from app.domain.ports import DeletionRepository as DeletionRepositoryPort

from ..transactions import run_in_transaction
from .task_repository import (
    _PENDING_STATE_PLACEHOLDERS,
    _PENDING_STATE_VALUES,
    _TASK_COLUMNS,
    SQLiteTaskRepository,
    ensure_queue_capacity,
    insert_task_event,
)

# 审计事件类型：与任务仓储共用同一套取值
_EVENT_CREATED = "created"


class SQLiteDeletionRepository(DeletionRepositoryPort):
    """knowledge_bases / documents / tasks 的删除组合写入

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def create_kb_deletion(
        self, kb_id: str, *, idempotency_key: str | None = None
    ) -> Task:
        """软删除知识库并创建清理任务（方法契约见领域 Port 定义）"""

        def _delete(conn) -> Task:
            # 幂等重放优先于一切写入：同键命中直接返回既有任务
            if idempotency_key is not None:
                existing = conn.execute(
                    f"SELECT {_TASK_COLUMNS} FROM tasks WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return SQLiteTaskRepository._to_entity(existing)

            row = conn.execute(
                "SELECT deleted_at FROM knowledge_bases WHERE id = ?", (kb_id,)
            ).fetchone()
            if row is None:
                raise EntityNotFoundError(f"知识库不存在: {kb_id}")
            if row[0] is not None:
                raise TaskStateConflictError(f"知识库已处于删除状态: {kb_id}")

            ensure_queue_capacity(conn)

            now = utc_now_iso()
            conn.execute(
                "UPDATE knowledge_bases"
                " SET deleted_at = ?, delete_requested_at = ?,"
                "     status = ?, updated_at = ?"
                " WHERE id = ? AND deleted_at IS NULL",
                (now, now, KnowledgeBaseStatus.DELETED.value, now, kb_id),
            )
            return self._insert_deletion_task(
                conn,
                task_type="delete_kb",
                knowledge_base_id=kb_id,
                document_id=None,
                idempotency_key=idempotency_key,
                now=now,
            )

        return run_in_transaction(
            self._conn, _delete, f"删除知识库 {kb_id}"
        )

    def create_document_deletion(
        self, document_id: str, *, idempotency_key: str | None = None
    ) -> Task:
        """软删除文档并创建清理任务（方法契约见领域 Port 定义）"""

        def _delete(conn) -> Task:
            # 幂等重放优先于一切写入：同键命中直接返回既有任务
            if idempotency_key is not None:
                existing = conn.execute(
                    f"SELECT {_TASK_COLUMNS} FROM tasks WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return SQLiteTaskRepository._to_entity(existing)

            row = conn.execute(
                "SELECT deleted_at, knowledge_base_id FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            if row is None or row[0] is not None:
                raise EntityNotFoundError(f"文档不存在或已删除: {document_id}")

            # 在途守卫：导入/重建未完成时删除会让版本演化不可追溯
            pending = conn.execute(
                "SELECT COUNT(*) FROM tasks"
                " WHERE document_id = ?"
                f" AND state IN ({_PENDING_STATE_PLACEHOLDERS})",
                (document_id, *_PENDING_STATE_VALUES),
            ).fetchone()[0]
            if pending > 0:
                raise TaskStateConflictError(
                    f"文档存在 {pending} 个未完成任务，暂不能删除"
                )

            ensure_queue_capacity(conn)

            now = utc_now_iso()
            conn.execute(
                "UPDATE documents"
                " SET deleted_at = ?, delete_requested_at = ?,"
                "     status = ?, updated_at = ?"
                " WHERE id = ? AND deleted_at IS NULL",
                (now, now, DocumentStatus.DELETED.value, now, document_id),
            )
            return self._insert_deletion_task(
                conn,
                task_type="delete_document",
                knowledge_base_id=row[1],
                document_id=document_id,
                idempotency_key=idempotency_key,
                now=now,
            )

        return run_in_transaction(
            self._conn, _delete, f"删除文档 {document_id}"
        )

    @staticmethod
    def _insert_deletion_task(
        conn,
        *,
        task_type: str,
        knowledge_base_id: str | None,
        document_id: str | None,
        idempotency_key: str | None,
        now: str,
    ) -> Task:
        """插入删除任务并写入创建事件，回读任务实体"""
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
            " VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, 0, 0, ?, 0, 3, 0, 0, 0,"
            "  NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,"
            "  ?, NULL, NULL)",
            (
                task_id, task_type, knowledge_base_id, document_id,
                TaskStatus.QUEUED.value, idempotency_key, now,
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
        row = conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return SQLiteTaskRepository._to_entity(row)
