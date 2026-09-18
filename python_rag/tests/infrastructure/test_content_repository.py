# -*- coding: utf-8 -*-
"""内容仓储测试：解析事实落库、表格证据、幂等重建与错误路径"""
import pytest

from app.domain.errors import EntityNotFoundError
from app.domain.parsing import BlockType, ParsedDocument, TableEvidence, make_block
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import SQLiteContentRepository

from .schema_helpers import (
    fresh_db,
    insert_block,
    insert_document,
    insert_kb,
    insert_table,
    insert_version,
)


@pytest.fixture()
def content_repo(tmp_path):
    """应用全部迁移的临时库 + 内容仓储与连接"""
    db_path, _ = fresh_db(tmp_path, name="content_repo.db")
    conn = connect(db_path)
    yield SQLiteContentRepository(conn), conn
    conn.close()


@pytest.fixture()
def version_id(content_repo):
    """真实外键链路上的文档版本 ID"""
    _, conn = content_repo
    kb_id = insert_kb(conn, "kb")
    doc_id = insert_document(conn, kb_id, "a" * 64)
    return insert_version(conn, doc_id)


def _parsed_document(version_id: str, *, with_table: bool = False) -> ParsedDocument:
    """构造统一解析模型样例（可选表格块）"""
    blocks = [
        make_block(BlockType.HEADING, 0, text="第一章"),
        make_block(BlockType.PARAGRAPH, 1, text="正文内容", page_no=1),
    ]
    if with_table:
        blocks.append(
            make_block(
                BlockType.TABLE,
                2,
                page_no=1,
                table=TableEvidence(
                    raw_html="<table><tr><td>甲</td></tr></table>",
                    raw_markdown="| 甲 |",
                    structure_json='{"rows": [["甲"]]}',
                    searchable_text="甲",
                    serialization_model="local",
                    serialization_version="1",
                ),
            )
        )
    return ParsedDocument(
        document_version_id=version_id,
        parser_provider="local",
        parser_name="test_parser",
        parser_version="1.0.0",
        blocks=tuple(blocks),
    )


def test_replace_writes_blocks_and_table_evidence(content_repo, version_id):
    """解析事实按块序落库，表格证据与块一对一写入"""
    repo, conn = content_repo
    parsed = _parsed_document(version_id, with_table=True)

    count = repo.replace_document_content(version_id, parsed)

    assert count == 3
    rows = conn.execute(
        "SELECT block_type, ordinal, content_text, page_no, content_hash"
        " FROM content_blocks WHERE document_version_id = ? ORDER BY ordinal",
        (version_id,),
    ).fetchall()
    assert [row[0] for row in rows] == ["heading", "paragraph", "table"]
    assert [row[1] for row in rows] == [0, 1, 2]
    assert rows[0][2] == "第一章"
    assert rows[1][3] == 1

    table = conn.execute(
        "SELECT raw_html, searchable_text, serialization_model, serialization_version"
        " FROM tables WHERE block_id = (SELECT id FROM content_blocks"
        "  WHERE document_version_id = ? AND block_type = 'table')",
        (version_id,),
    ).fetchone()
    assert table[0] == "<table><tr><td>甲</td></tr></table>"
    assert table[1] == "甲"
    assert (table[2], table[3]) == ("local", "1")


def test_replay_is_idempotent(content_repo, version_id):
    """同一解析产物重放：先删后插，不产生重复块与重复表格证据"""
    repo, conn = content_repo
    parsed = _parsed_document(version_id, with_table=True)

    repo.replace_document_content(version_id, parsed)
    repo.replace_document_content(version_id, parsed)

    blocks = conn.execute(
        "SELECT COUNT(*) FROM content_blocks WHERE document_version_id = ?",
        (version_id,),
    ).fetchone()[0]
    tables = conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0]
    assert (blocks, tables) == (3, 1)


def test_replace_discards_previous_content(content_repo, version_id):
    """重建覆盖既有内容：旧块与旧表格证据一并清除"""
    repo, conn = content_repo
    stale_block = insert_block(conn, version_id, ordinal=9, content_text="旧内容")
    insert_table(conn, stale_block)

    repo.replace_document_content(version_id, _parsed_document(version_id))

    remaining = conn.execute(
        "SELECT content_text FROM content_blocks WHERE document_version_id = ?",
        (version_id,),
    ).fetchall()
    assert [row[0] for row in remaining] == ["第一章", "正文内容"]
    assert conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0] == 0


def test_empty_block_sequence_writes_nothing(content_repo, version_id):
    """空块序列是合法产物：落库后无任何内容行"""
    repo, conn = content_repo
    empty = ParsedDocument(
        document_version_id=version_id,
        parser_provider="local",
        parser_name="test_parser",
        parser_version="1.0.0",
        blocks=(),
    )

    count = repo.replace_document_content(version_id, empty)

    assert count == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM content_blocks WHERE document_version_id = ?",
        (version_id,),
    ).fetchone()[0] == 0


def test_missing_version_rejected(content_repo):
    """版本不存在时重建解析事实抛领域错误"""
    repo, _ = content_repo
    unknown = "01900000-0000-7000-8000-000000000000"
    with pytest.raises(EntityNotFoundError):
        repo.replace_document_content(unknown, _parsed_document(unknown))
