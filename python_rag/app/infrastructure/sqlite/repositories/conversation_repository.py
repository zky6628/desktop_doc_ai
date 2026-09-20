# -*- coding: utf-8 -*-
"""会话仓储的 SQLite 实现（查询链路：会话、消息与历史读取）

会话删除为存储层级联：messages/citations 随外联清除，query_runs 的
会话与消息关联列由外键 ON DELETE SET NULL 置空（查询指标事实保留）。
"""
from app.domain.clock import utc_now_iso
from app.domain.entities import ConversationMessage, ConversationSummary
from app.domain.errors import EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.ports import ConversationRepository as ConversationRepositoryPort

from ..transactions import run_in_transaction

# 最后消息摘要的截取长度（换行折叠为空格后按字符截断）
_EXCERPT_MAX_CHARS = 120


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

    def list_by_knowledge_base(
        self,
        kb_id: str,
        *,
        limit: int = 50,
        after_updated_at: str | None = None,
        after_id: str | None = None,
    ) -> list[ConversationSummary]:
        """列出知识库内会话（方法契约见领域 Port 定义）"""
        conditions = ["knowledge_base_id = ?"]
        params: list[object] = [kb_id]
        if after_updated_at is not None and after_id is not None:
            conditions.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend([after_updated_at, after_updated_at, after_id])
        params.append(limit)
        rows = self._conn.execute(
            "SELECT id, knowledge_base_id, title, created_at, updated_at"
            " FROM conversations"
            f" WHERE {' AND '.join(conditions)}"
            " ORDER BY updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        summaries = self._summaries_with_last_messages(rows)
        return summaries

    def _summaries_with_last_messages(
        self, rows: list
    ) -> list[ConversationSummary]:
        """把会话页行构造为摘要（窗口函数取每会话最后一条消息）"""
        conversation_ids = [row[0] for row in rows]
        last_by_conversation: dict[str, tuple] = {}
        if conversation_ids:
            placeholders = ", ".join("?" * len(conversation_ids))
            message_rows = self._conn.execute(
                "SELECT conversation_id, role, content, created_at FROM ("
                " SELECT conversation_id, role, content, created_at,"
                " ROW_NUMBER() OVER ("
                "  PARTITION BY conversation_id ORDER BY created_at DESC, id DESC"
                " ) AS rn"
                f" FROM messages WHERE conversation_id IN ({placeholders})"
                ") WHERE rn = 1",
                conversation_ids,
            ).fetchall()
            last_by_conversation = {row[0]: row for row in message_rows}
        summaries = []
        for row in rows:
            last = last_by_conversation.get(row[0])
            summaries.append(
                ConversationSummary(
                    id=row[0],
                    knowledge_base_id=row[1],
                    title=row[2],
                    created_at=row[3],
                    updated_at=row[4],
                    last_message_role=last[1] if last else None,
                    last_message_excerpt=_excerpt(last[2]) if last else None,
                    last_message_created_at=last[3] if last else None,
                )
            )
        return summaries

    def list_messages(self, conversation_id: str) -> list[ConversationMessage]:
        """按创建时间升序读取会话全部消息（方法契约见领域 Port 定义）"""

        def _list(conn) -> list[ConversationMessage]:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if exists is None:
                raise EntityNotFoundError(f"会话不存在: {conversation_id}")
            rows = conn.execute(
                "SELECT id, conversation_id, role, content, created_at"
                " FROM messages WHERE conversation_id = ?"
                " ORDER BY created_at, id",
                (conversation_id,),
            ).fetchall()
            return [ConversationMessage(*row) for row in rows]

        return run_in_transaction(
            self._conn, _list, f"读取会话消息 {conversation_id}"
        )

    def delete(self, conversation_id: str) -> None:
        """删除会话（方法契约见领域 Port 定义）"""

        def _delete(conn) -> None:
            cursor = conn.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
            if cursor.rowcount == 0:
                raise EntityNotFoundError(f"会话不存在: {conversation_id}")

        return run_in_transaction(
            self._conn, _delete, f"删除会话 {conversation_id}"
        )


def _excerpt(content: str) -> str:
    """折叠空白后按长度截取摘要（不附加省略号，展示交给客户端）"""
    collapsed = " ".join(content.split())
    return collapsed[:_EXCERPT_MAX_CHARS]
