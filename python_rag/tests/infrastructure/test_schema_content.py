# -*- coding: utf-8 -*-
"""
内容表 schema 测试（content_blocks / tables / chunks / chunk_block_links）：

- 表与唯一索引就位
- (index_version_id, ordinal) 唯一
- parent_chunk_id ON DELETE SET NULL
- chunk_block_links 联合主键与级联删除
- 引用定位链 chunk -> link -> block -> table 可达
"""
import sqlite3

import pytest

from .schema_helpers import (
    fresh_db,
    insert_block,
    insert_chunk,
    insert_chunk_block_link,
    insert_document,
    insert_index_version,
    insert_kb,
    insert_table,
    insert_version,
)


def _fresh_db(tmp_path):
    """创建应用过全部迁移的临时库（测试辅助）"""
    db_path, applied = fresh_db(tmp_path, name="schema_content.db")
    assert applied == 9
    return db_path


def _content_chain(tmp_path):
    """构建 KB -> Document -> Version -> IndexVersion 的完整链路（测试辅助）"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    kb_id = insert_kb(conn, "kb")
    doc_id = insert_document(conn, kb_id, "5" * 64)
    version_id = insert_version(conn, doc_id, 1)
    index_version_id = insert_index_version(conn, version_id, status="active")
    return conn, version_id, index_version_id


def test_content_tables_apply(tmp_path):
    """内容表与唯一索引就位"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"content_blocks", "tables", "chunks", "chunk_block_links"} <= tables

        indexes = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"uq_chunks_version_ordinal"} <= indexes
    finally:
        conn.close()


def test_chunk_ordinal_unique_per_index_version(tmp_path):
    """同一索引版本内切片序号唯一；不同索引版本独立编号"""
    conn, _, index_version_id = _content_chain(tmp_path)
    try:
        insert_chunk(conn, index_version_id, 1)
        with pytest.raises(sqlite3.IntegrityError):
            insert_chunk(conn, index_version_id, 1)

        # 另一个索引版本内序号重新从 1 开始
        version_id = conn.execute(
            "SELECT document_version_id FROM index_versions WHERE id = ?",
            (index_version_id,),
        ).fetchone()[0]
        other_index = insert_index_version(conn, version_id, status="staging")
        insert_chunk(conn, other_index, 1)
    finally:
        conn.close()


def test_chunk_parent_set_null_on_delete(tmp_path):
    """父切片删除后，子切片的 parent_chunk_id 置空而非报错"""
    conn, _, index_version_id = _content_chain(tmp_path)
    try:
        parent_id = insert_chunk(conn, index_version_id, 1, content="父切片")
        child_id = insert_chunk(conn, index_version_id, 2, parent_chunk_id=parent_id)

        conn.execute("DELETE FROM chunks WHERE id = ?", (parent_id,))

        remaining = conn.execute(
            "SELECT parent_chunk_id FROM chunks WHERE id = ?", (child_id,)
        ).fetchone()
        assert remaining == (None,)
    finally:
        conn.close()


def test_chunk_block_link_composite_pk(tmp_path):
    """(chunk_id, block_id) 联合主键：同对重复拒绝；不同组合合法"""
    conn, version_id, index_version_id = _content_chain(tmp_path)
    try:
        block_a = insert_block(conn, version_id, ordinal=1)
        block_b = insert_block(conn, version_id, ordinal=2)
        chunk_id = insert_chunk(conn, index_version_id, 1)

        insert_chunk_block_link(conn, chunk_id, block_a)
        with pytest.raises(sqlite3.IntegrityError):
            insert_chunk_block_link(conn, chunk_id, block_a)

        # 同切片可关联多个块；同块可被多个切片关联
        insert_chunk_block_link(conn, chunk_id, block_b)
        other_chunk = insert_chunk(conn, index_version_id, 2)
        insert_chunk_block_link(conn, other_chunk, block_a)
    finally:
        conn.close()


def test_chunk_delete_cascades_links(tmp_path):
    """切片删除时其定位关系随级联消失"""
    conn, version_id, index_version_id = _content_chain(tmp_path)
    try:
        block_id = insert_block(conn, version_id, ordinal=1)
        chunk_id = insert_chunk(conn, index_version_id, 1)
        insert_chunk_block_link(conn, chunk_id, block_id)

        conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))

        remaining = conn.execute(
            "SELECT COUNT(*) FROM chunk_block_links WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()[0]
        assert remaining == 0
        # 解析块本身不受影响
        assert conn.execute(
            "SELECT COUNT(*) FROM content_blocks WHERE id = ?", (block_id,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_table_one_to_one_with_block(tmp_path):
    """tables 与 content_blocks 一对一：同一 block_id 只能有一条表格记录"""
    conn, version_id, _ = _content_chain(tmp_path)
    try:
        block_id = insert_block(conn, version_id, block_type="table", ordinal=1)
        insert_table(conn, block_id)
        with pytest.raises(sqlite3.IntegrityError):
            insert_table(conn, block_id)
    finally:
        conn.close()


def test_citation_locator_chain(tmp_path):
    """引用定位链可达：chunk -> chunk_block_links -> content_blocks -> tables"""
    conn, version_id, index_version_id = _content_chain(tmp_path)
    try:
        text_block = insert_block(conn, version_id, block_type="paragraph", ordinal=1)
        table_block = insert_block(conn, version_id, block_type="table", ordinal=2)
        insert_table(conn, table_block)
        chunk_id = insert_chunk(conn, index_version_id, 1)
        insert_chunk_block_link(conn, chunk_id, text_block)
        insert_chunk_block_link(conn, chunk_id, table_block, relation_type="exact")

        # 从切片出发，经定位关系取到关联的表格证据
        rows = conn.execute(
            "SELECT cb.block_type, t.searchable_text"
            " FROM chunks c"
            " JOIN chunk_block_links cbl ON cbl.chunk_id = c.id"
            " JOIN content_blocks cb ON cb.id = cbl.block_id"
            " LEFT JOIN tables t ON t.block_id = cb.id"
            " WHERE c.id = ? ORDER BY cb.ordinal",
            (chunk_id,),
        ).fetchall()

        assert rows == [
            ("paragraph", None),
            ("table", "表格可检索文本"),
        ]
    finally:
        conn.close()


def test_foreign_key_check_clean(tmp_path):
    """PRAGMA foreign_key_check 无违规"""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

