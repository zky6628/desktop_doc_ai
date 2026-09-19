# -*- coding: utf-8 -*-
"""引用快照仓储测试：批量幂等写入、读取回填与缺失消息拒绝"""
import pytest

from app.domain.citation import (
    STATE_REJECTED,
    STATE_VALIDATED,
    CitationRecord,
)
from app.domain.errors import EntityNotFoundError
from app.domain.ids import uuid7
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import SQLiteCitationRepository

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
def env(tmp_path):
    """临时库 + 引用快照仓储 + 预置消息链路与真实切片"""
    db_path, applied = fresh_db(tmp_path, name="citation_repo.db")
    connection = connect(db_path)
    kb_id = insert_kb(connection, "kb")
    doc_id = insert_document(connection, kb_id, "a" * 64)
    version_id = insert_version(connection, doc_id)
    index_id = insert_index_version(connection, version_id)
    chunk_id = insert_chunk(connection, index_id, ordinal=0, content="切片内容")
    conversation_id = uuid7()
    connection.execute(
        "INSERT INTO conversations (id, knowledge_base_id, title, created_at, updated_at)"
        " VALUES (?, ?, NULL, ?, ?)",
        (conversation_id, kb_id, FIXED_TIME, FIXED_TIME),
    )
    message_id = uuid7()
    connection.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at)"
        " VALUES (?, ?, 'assistant', '回答正文', ?)",
        (message_id, conversation_id, FIXED_TIME),
    )

    class Env:
        pass

    state = Env()
    state.conn = connection
    state.applied = applied
    state.message_id = message_id
    state.chunk_id = chunk_id
    state.repo = SQLiteCitationRepository(connection)
    yield state
    connection.close()


def _record(message_id: str, order: int, state: str = STATE_VALIDATED,
            chunk_id: str | None = None) -> CitationRecord:
    """构造快照记录（默认校验通过并指向真实切片，满足外键约束）"""
    return CitationRecord(
        assistant_message_id=message_id,
        citation_order=order,
        chunk_id=chunk_id,
        knowledge_base_id_snapshot="kb-1",
        document_id_snapshot="doc-1",
        document_version_id_snapshot="dv-1",
        file_name_snapshot="员工手册.pdf",
        version_no_snapshot=2,
        quoted_text_snapshot=f"引文{order}",
        page_no=12,
        section_path="休假制度/年假",
        source_locator_json='{"block_ids":["block-a"]}',
        validation_state=state,
        vector_score=0.81,
        keyword_score=7.42,
        fusion_score=0.031,
        rerank_score=0.92,
    )


def test_insert_and_list_roundtrip_with_order(env):
    """批量写入后按序号升序读取，主键与全部快照字段回填"""
    written = env.repo.insert_citations(
        [
            _record(env.message_id, 2, STATE_REJECTED, chunk_id=env.chunk_id),
            _record(env.message_id, 1, chunk_id=env.chunk_id),
        ]
    )

    assert written == 2
    records = env.repo.list_by_message(env.message_id)
    assert [record.citation_order for record in records] == [1, 2]
    assert all(record.citation_id for record in records)
    first = records[0]
    assert first.chunk_id == env.chunk_id
    assert first.file_name_snapshot == "员工手册.pdf"
    assert first.version_no_snapshot == 2
    assert first.quoted_text_snapshot == "引文1"
    assert first.validation_state == STATE_VALIDATED
    assert (first.vector_score, first.rerank_score) == (0.81, 0.92)
    assert records[1].validation_state == STATE_REJECTED


def test_replay_same_order_is_idempotent(env):
    """同消息同序号重放忽略不覆盖，行数与内容不变"""
    env.repo.insert_citations([_record(env.message_id, 1, chunk_id=env.chunk_id)])

    written = env.repo.insert_citations(
        [_record(env.message_id, 1, chunk_id=env.chunk_id)]
    )

    assert written == 0
    records = env.repo.list_by_message(env.message_id)
    assert len(records) == 1
    assert records[0].quoted_text_snapshot == "引文1"


def test_rejected_record_without_chunk_pointer(env):
    """编号未知的拒绝快照可无切片指针写入（外键合法）"""
    written = env.repo.insert_citations(
        [_record(env.message_id, 1, STATE_REJECTED, chunk_id=None)]
    )

    assert written == 1
    record = env.repo.list_by_message(env.message_id)[0]
    assert record.chunk_id is None
    assert record.validation_state == STATE_REJECTED


def test_insert_missing_message_rejected(env):
    """消息不存在时写入抛领域错误"""
    with pytest.raises(EntityNotFoundError):
        env.repo.insert_citations([_record("missing-message", 1)])


def test_list_by_missing_message_returns_empty(env):
    """消息不存在时读取返回空列表"""
    assert env.repo.list_by_message("missing-message") == []
