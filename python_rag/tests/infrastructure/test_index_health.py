# -*- coding: utf-8 -*-
"""派生索引健康检查测试：健康/损坏/缺失/哈希漂移与孤儿资产报告

使用真实 SQLite 仓储与 Chroma 内存实例，切片与双索引按生产管线
语义构建（确定性切片 + 按记录 ID 幂等写入），再逐项制造损坏现场。
"""
from types import SimpleNamespace

import chromadb
import pytest

from app.domain.chunking import chunk_blocks, integrity_hash
from app.domain.ids import uuid7
from app.domain.index_maintenance import (
    ISSUE_CHUNK_COUNT_MISMATCH,
    ISSUE_CHUNK_FACTS_MISSING,
    ISSUE_INTEGRITY_HASH_MISMATCH,
    ISSUE_KEYWORD_COUNT_MISMATCH,
    ISSUE_VECTOR_COLLECTION_MISSING,
    ISSUE_VECTOR_ID_MISMATCH,
    IndexHealthService,
)
from app.domain.keyword import KeywordDocument
from app.infrastructure.keywordindex import JiebaTokenizer, SQLiteFtsKeywordIndex
from app.infrastructure.parsing import TxtMarkdownParser
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteConfigRepository,
    SQLiteContentRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteIndexVersionRepository,
    SQLiteKnowledgeBaseRepository,
)
from app.infrastructure.storage.upload_staging import UploadStagingStore
from app.infrastructure.vectorindex import ChromaVectorIndexAdapter

from .schema_helpers import ensure_pipeline_configs, fresh_db

# 两段超预算段落：保证产出多个子切片（父内按子预算再分）
_LONG_PARAGRAPH = "字" * 500


@pytest.fixture()
def env(tmp_path):
    """临时库 + 全套仓储 + 健康检查服务"""
    db_path, _ = fresh_db(tmp_path, name="index_health.db")
    conn = connect(db_path)
    kb = SQLiteKnowledgeBaseRepository(conn).create(name="测试知识库")
    repos = {
        "documents": SQLiteDocumentRepository(conn),
        "versions": SQLiteDocumentVersionRepository(conn),
        "content": SQLiteContentRepository(conn),
        "indexes": SQLiteIndexVersionRepository(conn),
        "chunks": SQLiteChunkRepository(conn),
        "configs": SQLiteConfigRepository(conn),
    }
    vector_index = ChromaVectorIndexAdapter(
        # 健康检查做全量孤儿扫描：每个测试用独立持久化目录隔离实例，
        # 避免进程内共享内存实例导致跨测试集合互渗
        chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    )
    keyword_index = SQLiteFtsKeywordIndex(conn)
    service = IndexHealthService(
        index_repo=repos["indexes"],
        chunk_repo=repos["chunks"],
        vector_index=vector_index,
        keyword_index=keyword_index,
    )
    yield SimpleNamespace(
        conn=conn,
        repos=repos,
        kb_id=kb.id,
        staging=str(tmp_path / "staging"),
        vector_index=vector_index,
        keyword_index=keyword_index,
        tokenizer=JiebaTokenizer(),
        service=service,
    )
    conn.close()


def _ensure_configs(env) -> tuple[str, str, str]:
    """确保切片/嵌入/关键词配置行存在（转发共享辅助），返回三个配置 ID"""
    return ensure_pipeline_configs(env.repos["configs"])


def _stage_version(env, content: str, filename: str = "健康.txt"):
    """经真实暂存建立文档版本并落库解析事实，返回文档版本"""
    staged = UploadStagingStore(env.staging).stage(iter([content.encode()]), filename)
    document = env.repos["documents"].create(env.kb_id, staged.display_name, staged.sha256)
    version = env.repos["versions"].create(
        document.id,
        staged.staging_path,
        staged.sha256,
        mime_type=staged.mime_type,
        size_bytes=staged.size_bytes,
    )
    parsed = TxtMarkdownParser(markdown_mode=False).parse(staged.staging_path, version.id)
    env.repos["content"].replace_document_content(version.id, parsed)
    return version


def _build_index(
    env,
    version_id: str,
    *,
    partial_vectors: bool = False,
    partial_keywords: bool = False,
) -> object:
    """按生产管线语义构建索引（切片落库 + 双索引写入 + 基准登记），返回索引行"""
    chunk_config_id, embed_config_id, keyword_config_id = _ensure_configs(env)
    index = env.repos["indexes"].create(
        version_id,
        chunking_config_id=chunk_config_id,
        embedding_profile_id=embed_config_id,
        keyword_config_id=keyword_config_id,
    )
    chunks = chunk_blocks(env.repos["content"].list_document_blocks(version_id))
    env.repos["chunks"].replace_index_chunks(index.id, chunks)
    stored = env.repos["chunks"].list_index_chunks(index.id)
    children = [s for s in stored if s.chunk.parent_ordinal is not None]
    assert len(children) >= 2

    collection = f"wb-idx-{index.id}"
    namespace = f"fts-{index.id}"
    env.repos["indexes"].set_vector_collection(index.id, collection)
    env.repos["indexes"].set_fts_namespace(index.id, namespace)

    vector_target = children[:1] if partial_vectors else children
    env.vector_index.upsert_vectors(
        collection, [s.id for s in vector_target], [[1.0] * 4] * len(vector_target)
    )
    keyword_target = children[:1] if partial_keywords else children
    token_lists = env.tokenizer.tokenize([s.chunk.content for s in keyword_target])
    env.keyword_index.rebuild_namespace(
        namespace,
        [
            KeywordDocument(chunk_id=s.id, content=" ".join(tokens))
            for s, tokens in zip(keyword_target, token_lists)
        ],
    )
    if not partial_vectors and not partial_keywords:
        env.repos["indexes"].record_validation(
            index.id,
            chunk_count=len(children),
            integrity_hash=integrity_hash([s.chunk for s in children]),
        )
    return index


def _activate(env, index) -> None:
    env.repos["indexes"].activate(index.id)


def test_healthy_active_index_reports_no_issues(env):
    """完整构建并激活的索引：健康结论为真且无孤儿"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)
    _activate(env, index)

    report = env.service.check_all()

    assert report.healthy is True
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.index_version_id == index.id
    assert finding.healthy is True
    assert finding.issues == ()
    assert finding.expected_child_count == finding.actual_vector_count
    assert finding.expected_child_count == finding.actual_keyword_count
    assert report.orphan_collections == ()
    assert report.orphan_namespaces == ()


def test_missing_collection_flagged(env):
    """向量集合整体缺失：检出集合缺失问题"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)
    _activate(env, index)
    env.vector_index.delete_collection(f"wb-idx-{index.id}")

    report = env.service.check_all()

    assert report.healthy is False
    assert ISSUE_VECTOR_COLLECTION_MISSING in report.findings[0].issues


def test_partial_vectors_flagged_with_id_diff(env):
    """向量仅写入部分子切片：检出 ID 失配并给出缺失清单"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id, partial_vectors=True)
    _activate(env, index)

    report = env.service.check_all()

    finding = report.findings[0]
    assert report.healthy is False
    assert ISSUE_VECTOR_ID_MISMATCH in finding.issues
    stored = env.repos["chunks"].list_index_chunks(index.id)
    children = [s for s in stored if s.chunk.parent_ordinal is not None]
    assert set(finding.missing_vector_ids) == {s.id for s in children[1:]}
    assert finding.unexpected_vector_ids == ()


def test_extra_vector_flagged(env):
    """集合中存在异物向量：检出 ID 失配并给出多余清单"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)
    _activate(env, index)
    env.vector_index.upsert_vectors(f"wb-idx-{index.id}", ["alien"], [[9.0] * 4])

    report = env.service.check_all()

    finding = report.findings[0]
    assert report.healthy is False
    assert ISSUE_VECTOR_ID_MISMATCH in finding.issues
    assert finding.unexpected_vector_ids == ("alien",)
    assert finding.missing_vector_ids == ()


def test_keyword_count_mismatch_flagged(env):
    """关键词索引仅重建了部分文档：检出数量失配"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id, partial_keywords=True)
    _activate(env, index)

    report = env.service.check_all()

    finding = report.findings[0]
    assert report.healthy is False
    assert ISSUE_KEYWORD_COUNT_MISMATCH in finding.issues


def test_integrity_hash_mismatch_flagged(env):
    """登记基准与切片事实漂移：检出完整性哈希失配"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)
    _activate(env, index)
    env.repos["indexes"].record_validation(
        index.id, chunk_count=env.repos["indexes"].get(index.id).chunk_count,
        integrity_hash="0" * 64,
    )

    report = env.service.check_all()

    assert ISSUE_INTEGRITY_HASH_MISMATCH in report.findings[0].issues


def test_chunk_count_mismatch_flagged(env):
    """登记基准与切片行数漂移：检出数量失配"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)
    _activate(env, index)
    env.repos["indexes"].record_validation(
        index.id, chunk_count=999,
        integrity_hash=env.repos["indexes"].get(index.id).integrity_hash,
    )

    report = env.service.check_all()

    assert ISSUE_CHUNK_COUNT_MISMATCH in report.findings[0].issues


def test_chunk_facts_missing_flagged(env):
    """活动索引没有任何切片事实：检出事实缺失"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)
    _activate(env, index)
    env.repos["chunks"].delete_index_chunks(index.id)

    report = env.service.check_all()

    finding = report.findings[0]
    assert report.healthy is False
    assert ISSUE_CHUNK_FACTS_MISSING in finding.issues


def test_orphan_and_unexpected_assets_reported(env):
    """孤儿集合/命名空间按命名合同检出，命名不符的只报告不动"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)
    _activate(env, index)

    orphan_collection = f"wb-idx-{uuid7()}"
    orphan_namespace = f"fts-{uuid7()}"
    env.vector_index.upsert_vectors(orphan_collection, ["orphan"], [[1.0] * 4])
    env.keyword_index.rebuild_namespace(
        orphan_namespace, [KeywordDocument(chunk_id="c", content="孤儿")]
    )
    env.vector_index.upsert_vectors("legacy-store", ["x"], [[1.0] * 4])
    env.keyword_index.rebuild_namespace(
        "global", [KeywordDocument(chunk_id="y", content="全局")]
    )

    report = env.service.check_all()

    assert report.orphan_collections == (orphan_collection,)
    assert report.orphan_namespaces == (orphan_namespace,)
    assert report.unexpected_collections == ("legacy-store",)
    assert report.unexpected_namespaces == ("global",)
    # 清洁度问题不影响检索健康结论
    assert report.healthy is True


def test_staging_index_not_checked_but_assets_referenced(env):
    """在途 staging 索引不参与健康比对，其登记名不算孤儿"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    active = _build_index(env, version.id)
    _activate(env, active)

    staging = _build_index(env, version.id)  # 同版本第二个索引（staging）
    env.vector_index.upsert_vectors(
        f"wb-idx-{staging.id}", ["staging-record"], [[1.0] * 4]
    )

    report = env.service.check_all()

    assert [f.index_version_id for f in report.findings] == [active.id]
    assert report.healthy is True
    assert report.orphan_collections == ()
