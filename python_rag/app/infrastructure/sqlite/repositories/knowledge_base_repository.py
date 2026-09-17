# -*- coding: utf-8 -*-
"""知识库仓储的 SQLite 实现"""
from app.domain.clock import utc_now_iso
from app.domain.entities import KnowledgeBase, KnowledgeBaseStatus
from app.domain.errors import DuplicateActiveNameError, EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.ports import KnowledgeBaseRepository as KnowledgeBaseRepositoryPort

from ..transactions import run_in_transaction


class SQLiteKnowledgeBaseRepository(KnowledgeBaseRepositoryPort):
    """knowledge_bases 表的仓储实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def create(self, name: str, description: str | None = None) -> KnowledgeBase:
        """创建知识库；活动名称唯一，冲突时翻译为领域错误"""

        def _create(conn) -> KnowledgeBase:
            now = utc_now_iso()
            duplicate = conn.execute(
                "SELECT 1 FROM knowledge_bases WHERE name = ? AND deleted_at IS NULL",
                (name,),
            ).fetchone()
            if duplicate is not None:
                raise DuplicateActiveNameError(f"活动知识库名称已存在: {name}")
            kb_id = uuid7()
            conn.execute(
                "INSERT INTO knowledge_bases"
                " (id, name, description, status, deleted_at, delete_requested_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
                (kb_id, name, description, KnowledgeBaseStatus.ACTIVE.value, now, now),
            )
            return KnowledgeBase(
                id=kb_id,
                name=name,
                description=description,
                status=KnowledgeBaseStatus.ACTIVE,
                deleted_at=None,
                delete_requested_at=None,
                created_at=now,
                updated_at=now,
            )

        return run_in_transaction(self._conn, _create, f"创建知识库 {name}")

    def get(self, kb_id: str) -> KnowledgeBase | None:
        row = self._conn.execute(
            "SELECT id, name, description, status, deleted_at, delete_requested_at,"
            " created_at, updated_at"
            " FROM knowledge_bases WHERE id = ?",
            (kb_id,),
        ).fetchone()
        return self._to_entity(row) if row is not None else None

    def find_active_by_name(self, name: str) -> KnowledgeBase | None:
        row = self._conn.execute(
            "SELECT id, name, description, status, deleted_at, delete_requested_at,"
            " created_at, updated_at"
            " FROM knowledge_bases WHERE name = ? AND deleted_at IS NULL",
            (name,),
        ).fetchone()
        return self._to_entity(row) if row is not None else None

    def list_active(self) -> list:
        rows = self._conn.execute(
            "SELECT id, name, description, status, deleted_at, delete_requested_at,"
            " created_at, updated_at"
            " FROM knowledge_bases WHERE deleted_at IS NULL"
            " ORDER BY created_at DESC, id"
        ).fetchall()
        return [self._to_entity(row) for row in rows]

    def soft_delete(self, kb_id: str) -> None:
        def _soft_delete(conn) -> None:
            now = utc_now_iso()
            cursor = conn.execute(
                "UPDATE knowledge_bases"
                " SET deleted_at = ?, delete_requested_at = ?,"
                "     status = ?, updated_at = ?"
                " WHERE id = ? AND deleted_at IS NULL",
                (now, now, KnowledgeBaseStatus.DELETED.value, now, kb_id),
            )
            if cursor.rowcount == 0:
                raise EntityNotFoundError(f"知识库不存在或已删除: {kb_id}")

        run_in_transaction(self._conn, _soft_delete, f"软删除知识库 {kb_id}")

    @staticmethod
    def _to_entity(row) -> KnowledgeBase:
        """把查询行转换为领域实体"""
        return KnowledgeBase(
            id=row[0],
            name=row[1],
            description=row[2],
            status=KnowledgeBaseStatus(row[3]),
            deleted_at=row[4],
            delete_requested_at=row[5],
            created_at=row[6],
            updated_at=row[7],
        )
