# -*- coding: utf-8 -*-
"""版本与索引版本仓储集成测试（含激活事务与双向一致性校验）"""
import pytest

from app.domain.errors import (
    ActivationError,
    EntityNotFoundError,
)
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteIndexVersionRepository,
    SQLiteKnowledgeBaseRepository,
)

from .schema_helpers import fresh_db


@pytest.fixture()
def repos(tmp_path):
    """应用全部迁移的临时库 + 全套仓储"""
    db_path, _ = fresh_db(tmp_path, name="repo_versions.db")
    conn = connect(db_path)
    repositories = {
        "kb": SQLiteKnowledgeBaseRepository(conn),
        "documents": SQLiteDocumentRepository(conn),
        "versions": SQLiteDocumentVersionRepository(conn),
        "indexes": SQLiteIndexVersionRepository(conn),
    }
    yield repositories, conn
    conn.close()


def _make_document(repositories) -> tuple:
    """构建 知识库 -> 文档 -> 版本 链路，返回 (version_id, doc_id)"""
    kb = repositories["kb"].create(name="kb")
    doc = repositories["documents"].create(kb.id, "报告.pdf", "a" * 64)
    version = repositories["versions"].create(doc.id, "uploads/报告.pdf", "a" * 64)
    return version.id, doc.id


def test_version_no_auto_increment(repos):
    """版本号在文档内自动递增；不同文档独立编号"""
    repositories, _ = repos
    kb = repositories["kb"].create(name="kb")
    doc_a = repositories["documents"].create(kb.id, "a.pdf", "a" * 64)
    doc_b = repositories["documents"].create(kb.id, "b.pdf", "b" * 64)

    v1 = repositories["versions"].create(doc_a.id, "p1", "a1" * 32)
    v2 = repositories["versions"].create(doc_a.id, "p2", "a2" * 32)
    other = repositories["versions"].create(doc_b.id, "p3", "a3" * 32)

    assert (v1.version_no, v2.version_no, other.version_no) == (1, 2, 1)

    listed = repositories["versions"].list_by_document(doc_a.id)
    assert [v.version_no for v in listed] == [1, 2]


def test_create_version_in_missing_document_rejected(repos):
    """文档不存在时创建版本抛领域错误"""
    repositories, _ = repos
    with pytest.raises(EntityNotFoundError):
        repositories["versions"].create(
            "01900000-0000-7000-8000-000000000000", "p", "a" * 64
        )


def test_mark_parsed_writes_back_parsing_facts(repos):
    """解析结果回写：待解析版本置为 parsed，哈希与解析器身份落库"""
    repositories, conn = repos
    version_id, _ = _make_document(repositories)
    # 模拟导入建立时的待解析状态
    conn.execute(
        "UPDATE document_versions SET status = 'pending' WHERE id = ?",
        (version_id,),
    )

    marked = repositories["versions"].mark_parsed(
        version_id,
        parsed_content_sha256="c" * 64,
        parser_provider="local",
        parser_version="1.0.0",
    )

    assert marked.status == "parsed"
    assert marked.parsed_content_sha256 == "c" * 64
    assert marked.parser_provider == "local"
    assert marked.parser_version == "1.0.0"

    loaded = repositories["versions"].get(version_id)
    assert loaded.status == "parsed"
    assert loaded.parsed_content_sha256 == "c" * 64


def test_mark_parsed_missing_version_rejected(repos):
    """版本不存在时回写解析结果抛领域错误"""
    repositories, _ = repos
    with pytest.raises(EntityNotFoundError):
        repositories["versions"].mark_parsed(
            "01900000-0000-7000-8000-000000000000",
            parsed_content_sha256="c" * 64,
            parser_provider="local",
            parser_version="1.0.0",
        )


def test_activate_switches_active_and_backfills_pointer(repos):
    """激活事务：新索引激活、旧索引退役、文档版本指针回填并双向一致"""
    repositories, _ = repos
    version_id, _ = _make_document(repositories)
    first = repositories["indexes"].create(version_id)
    second = repositories["indexes"].create(version_id)

    activated_first = repositories["indexes"].activate(first.id)
    assert activated_first.status.value == "active"
    assert activated_first.activated_at is not None

    # 指针指向第一个索引
    loaded_version = repositories["versions"].get(version_id)
    assert loaded_version.active_index_version_id == first.id

    # 激活第二个：第一个自动退役，指针切换
    activated_second = repositories["indexes"].activate(second.id)
    assert activated_second.status.value == "active"

    loaded_version = repositories["versions"].get(version_id)
    assert loaded_version.active_index_version_id == second.id

    retired_first = repositories["indexes"].get(first.id)
    assert retired_first.status.value == "retired"
    assert retired_first.retired_at is not None

    # 同一文档版本的两个 active 不可能出现（部分唯一索引兜底）
    actives = [
        idx.id
        for idx in repositories["indexes"].list_by_document_version(version_id)
        if idx.status.value == "active"
    ]
    assert actives == [second.id]


def test_activate_retired_index_rejected(repos):
    """retired/failed 状态不允许直接激活"""
    repositories, _ = repos
    version_id, _ = _make_document(repositories)
    first = repositories["indexes"].create(version_id)
    second = repositories["indexes"].create(version_id)

    repositories["indexes"].activate(first.id)
    repositories["indexes"].activate(second.id)

    with pytest.raises(ActivationError):
        repositories["indexes"].activate(first.id)


def test_activate_missing_index_rejected(repos):
    """激活不存在的索引版本抛领域错误"""
    repositories, _ = repos
    with pytest.raises(EntityNotFoundError):
        repositories["indexes"].activate("01900000-0000-7000-8000-000000000000")


def test_full_lifecycle_integration(repos):
    """集成场景：知识库 -> 文档 -> 版本 -> 索引激活 -> 软删除互不破坏"""
    repositories, _ = repos
    kb = repositories["kb"].create(name="项目资料")
    doc = repositories["documents"].create(kb.id, "报告.pdf", "a" * 64)
    version = repositories["versions"].create(doc.id, "uploads/报告.pdf", "a" * 64)
    index = repositories["indexes"].create(version.id)
    repositories["indexes"].activate(index.id)
    repositories["documents"].set_active_version(doc.id, version.id)

    # 删除同 KB 内另一个无关文档不影响链路
    other = repositories["documents"].create(kb.id, "其他.pdf", "b" * 64)
    repositories["documents"].soft_delete(other.id)

    doc_loaded = repositories["documents"].get(doc.id)
    version_loaded = repositories["versions"].get(version.id)
    index_loaded = repositories["indexes"].get(index.id)

    assert doc_loaded.active_document_version_id == version.id
    assert version_loaded.active_index_version_id == index.id
    assert index_loaded.status.value == "active"
    assert doc_loaded.status.value == "ready"
