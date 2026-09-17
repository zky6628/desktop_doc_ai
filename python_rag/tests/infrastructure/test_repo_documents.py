# -*- coding: utf-8 -*-
"""文档仓储集成测试（真实临时 SQLite 库）"""
import pytest

from app.domain.errors import (
    DuplicateActiveContentError,
    EntityNotFoundError,
)
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteKnowledgeBaseRepository,
)

from .schema_helpers import fresh_db


@pytest.fixture()
def repo(tmp_path):
    """应用全部迁移的临时库 + 文档/版本/知识库仓储"""
    db_path, _ = fresh_db(tmp_path, name="repo_doc.db")
    conn = connect(db_path)
    yield {
        "documents": SQLiteDocumentRepository(conn),
        "versions": SQLiteDocumentVersionRepository(conn),
        "kb": SQLiteKnowledgeBaseRepository(conn),
    }, conn
    conn.close()


def test_create_and_get(repo):
    """创建文档后可读回；初始状态为 queued"""
    repositories, _ = repo
    kb = repositories["kb"].create(name="kb")
    doc = repositories["documents"].create(kb.id, "报告.pdf", "a" * 64)

    assert doc.knowledge_base_id == kb.id
    assert doc.display_name == "报告.pdf"
    assert doc.status.value == "queued"
    assert doc.active_document_version_id is None

    assert repositories["documents"].get(doc.id) == doc


def test_create_duplicate_active_content_rejected(repo):
    """同知识库内活动内容唯一：重复导入抛领域错误"""
    repositories, _ = repo
    kb = repositories["kb"].create(name="kb")
    repositories["documents"].create(kb.id, "报告.pdf", "a" * 64)
    with pytest.raises(DuplicateActiveContentError):
        repositories["documents"].create(kb.id, "副本.pdf", "a" * 64)


def test_create_in_missing_kb_rejected(repo):
    """知识库不存在时创建文档抛领域错误"""
    repositories, _ = repo
    with pytest.raises(EntityNotFoundError):
        repositories["documents"].create(
            "01900000-0000-7000-8000-000000000000", "x.pdf", "b" * 64
        )


def test_list_by_kb_excludes_deleted(repo):
    """默认列表排除软删除文档；显式包含时可读"""
    repositories, _ = repo
    kb = repositories["kb"].create(name="kb")
    kept = repositories["documents"].create(kb.id, "保留.pdf", "a" * 64)
    dropped = repositories["documents"].create(kb.id, "删除.pdf", "b" * 64)
    repositories["documents"].soft_delete(dropped.id)

    active_docs = repositories["documents"].list_by_kb(kb.id)
    assert [d.id for d in active_docs] == [kept.id]

    all_docs = repositories["documents"].list_by_kb(kb.id, include_deleted=True)
    assert {d.id for d in all_docs} == {kept.id, dropped.id}


def test_set_active_version_updates_pointer_and_status(repo):
    """回填活动版本指针：版本不存在抛错；成功后文档状态变为 ready"""
    repositories, _ = repo
    kb = repositories["kb"].create(name="kb")
    doc = repositories["documents"].create(kb.id, "报告.pdf", "a" * 64)
    version = repositories["versions"].create(doc.id, "uploads/报告.pdf", "a" * 64)

    repositories["documents"].set_active_version(doc.id, version.id)

    loaded = repositories["documents"].get(doc.id)
    assert loaded.active_document_version_id == version.id
    assert loaded.status.value == "ready"

    with pytest.raises(EntityNotFoundError):
        repositories["documents"].set_active_version(doc.id, "01900000-0000-7000-8000-000000000000")
