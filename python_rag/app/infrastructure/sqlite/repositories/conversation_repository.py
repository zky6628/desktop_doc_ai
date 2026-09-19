# -*- coding: utf-8 -*-
"""会话仓储的 SQLite 实现（查询链路最小面：确保会话与追加消息）"""
from app.domain.clock import utc_now_iso
from app.domain.errors import EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.ports import ConversationRepository as ConversationRepositoryPort

from ..transactions import run_in_transaction


class SQLiteConversationRepository(ConversationRepositoryPort):
    """conversations / messages 表的查询链路实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def ensure_conversation(self, kb_id: str, conversation_id: str | None) -> str:
        """确保会话存在并返回主键（方法契约见领域 Port 定义）"""

        def _ensure(conn) -> str:
            if conversation_id is not None:
                exists = conn.execute(
                    "SELECT 1 FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if exists is None:
                    raise EntityNotFoundError(f"会话不存在: {conversation_id}")
                return conversation_id
            exists = conn.execute(
                "SELECT 1 FROM knowledge_bases WHERE id = ?", (kb_id,)
            ).fetchone()
            if exists is None:
                raise EntityNotFoundError(f"知识库不存在: {kb_id}")
            new_id = uuid7()
            now = utc_now_iso()
            conn.execute(
                "INSERT INTO conversations (id, knowledge_base_id, title,"
                " created_at, updated_at)"
                " VALUES (?, ?, NULL, ?, ?)",
                (new_id, kb_id, now, now),
            )
            return new_id

        return run_in_transaction(self._conn, _ensure, "确保会话存在")

    def add_message(self, conversation_id: str, role: str, content: str) -> str:
        """追加消息并返回主键（方法契约见领域 Port 定义）"""

        def _add(conn) -> str:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if exists is None:
                raise EntityNotFoundError(f"会话不存在: {conversation_id}")
            message_id = uuid7()
            now = utc_now_iso()
            conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (message_id, conversation_id, role, content, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            return message_id

        return run_in_transaction(
            self._conn, _add, f"追加 {role} 消息 {conversation_id}"
        )

    def get_message_content(self, message_id: str) -> str | None:
        """按主键读取消息正文（方法契约见领域 Port 定义）"""
        row = self._conn.execute(
            "SELECT content FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return row[0] if row is not None else None
