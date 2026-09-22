# -*- coding: utf-8 -*-
"""会话族 schema 测试：迁移落地、外键行为与引用快照保留"""
import sqlite3

import pytest

from app.domain.ids import uuid7

from .schema_helpers import (
    FIXED_TIME,
    fresh_db,
    insert_chunk,
    insert_document,
    insert_index_version,
    insert_kb,
    insert_version,
)


@pytest.fixture()
def conn(tmp_path):
    db_path, applied = fresh_db(tmp_path, name="conversation_schema.db")
    from app.infrastructure.sqlite.connection import connect

    connection = connect(db_path)
    yield connection, applied
    connection.close()


def _conversation(conn, kb_id: str) -> str:
    conversation_id = uuid7()
    conn.execute(
        "INSERT INTO conversations (id, knowledge_base_id, title, created_at, updated_at)"
        " VALUES (?, ?, NULL, ?, ?)",
        (conversation_id, kb_id, FIXED_TIME, FIXED_TIME),
    )
    return conversation_id


def _message(conn, conversation_id: str, role: str = "assistant") -> str:
    message_id = uuid7()
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at)"
        " VALUES (?, ?, ?, '正文', ?)",
        (message_id, conversation_id, role, FIXED_TIME),
    )
    return message_id


def _citation(conn, message_id: str, chunk_id: str | None = None, order: int = 1) -> str:
    citation_id = uuid7()
    conn.execute(
        "INSERT INTO citations (id, assistant_message_id, chunk_id, citation_order,"
        " knowledge_base_id_snapshot, quoted_text_snapshot, validation_state, created_at)"
        " VALUES (?, ?, ?, ?, 'kb-1', '引文', 'validated', ?)",
        (citation_id, message_id, chunk_id, order, FIXED_TIME),
    )
    return citation_id


def test_migration_applies_full_set_including_conversations(tmp_path):
    """迁移全集应用：会话族三表落地且计数为 7（只增不减基准）"""
    db_path, applied = fresh_db(tmp_path, name="conversation_count.db")
    from app.infrastructure.sqlite.connection import connect

    connection = connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert applied == 13
        assert {"conversations", "messages", "citations"} <= tables
    finally:
        connection.close()


def test_chunk_delete_retains_citation_snapshot_with_null_pointer(conn):
    """切片清理后引用快照保留：切片指针置空，快照事实完整"""
    connection, _ = conn
    kb_id = insert_kb(connection, "kb")
    doc_id = insert_document(connection, kb_id, "a" * 64)
    version_id = insert_version(connection, doc_id)
    index_id = insert_index_version(connection, version_id)
    chunk_id = insert_chunk(connection, index_id, ordinal=0, content="被清理的切片")

    conversation_id = _conversation(connection, kb_id)
    message_id = _message(connection, conversation_id)
    citation_id = _citation(connection, message_id, chunk_id=chunk_id)

    connection.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))

    row = connection.execute(
        "SELECT chunk_id, file_name_snapshot, quoted_text_snapshot"
        " FROM citations WHERE id = ?",
        (citation_id,),
    ).fetchone()
    assert row == (None, None, "引文")


def test_message_delete_cascades_citations(conn):
    """消息删除时引用级联消失；会话删除时消息级联消失"""
    connection, _ = conn
    kb_id = insert_kb(connection, "kb")
    conversation_id = _conversation(connection, kb_id)
    message_id = _message(connection, conversation_id)
    _citation(connection, message_id)

    connection.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    remaining = connection.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
    assert remaining == 0

    connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_citation_order_unique_per_message(conn):
    """同消息内引用序号唯一（幂等重放的存储层兜底）"""
    connection, _ = conn
    kb_id = insert_kb(connection, "kb")
    conversation_id = _conversation(connection, kb_id)
    message_id = _message(connection, conversation_id)
    _citation(connection, message_id, order=1)

    with pytest.raises(sqlite3.IntegrityError):
        _citation(connection, message_id, order=1)


def test_message_role_is_constrained(conn):
    """消息角色约束为 user/assistant"""
    connection, _ = conn
    kb_id = insert_kb(connection, "kb")
    conversation_id = _conversation(connection, kb_id)

    with pytest.raises(sqlite3.IntegrityError):
        _message(connection, conversation_id, role="system")

