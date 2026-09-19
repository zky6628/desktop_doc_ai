# -*- coding: utf-8 -*-
"""双路检索服务测试：指针链召回、双路融合、候选事实校验与管线冒烟

集成状态一部分由 schema 助手手工搭建（精确控制切片/向量/分词内容），
一部分由真实导入管线产出（冒烟验证管线产物可检索）。查询嵌入用固定
向量替身，Chroma 与 FTS 均为真实适配器。
"""
from types import SimpleNamespace

import chromadb
import pytest

from app.domain.index_maintenance import collection_name, fts_namespace_name
from app.domain.keyword import KeywordDocument
from app.infrastructure.keywordindex import JiebaTokenizer, SQLiteFtsKeywordIndex
from app.infrastructure.retrieval import RetrievalService
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteIndexVersionRepository,
)
from app.infrastructure.vectorindex import ChromaVectorIndexAdapter

from .schema_helpers import (
    FIXED_TIME,
    fresh_db,
    import_file,
    insert_chunk,
    insert_document,
    insert_index_version,
    insert_kb,
    insert_version,
)


class FixedQueryEmbedder:
    """固定向量的查询嵌入替身（记录调用供断言）"""

    def __init__(self, vector: list[float]) -> None:
        self.vector = list(vector)
        self.calls: list[str] = []

    def embed_query(self, question: str) -> list[float]:
        self.calls.append(question)
        return list(self.vector)


@pytest.fixture()
def retrieval_env(tmp_path):
    """临时库 + 真实双索引适配器 + 可变查询向量的检索服务构建器"""
    db_path, _ = fresh_db(tmp_path, name="retrieval.db")
    conn = connect(db_path)
    vector_index = ChromaVectorIndexAdapter(
        chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    )
    keyword_index = SQLiteFtsKeywordIndex(conn)
    embedder = FixedQueryEmbedder([1.0, 0.0, 0.0, 0.0])

    def build_service(**overrides):
        return RetrievalService(
            index_repo=SQLiteIndexVersionRepository(conn),
            chunk_repo=SQLiteChunkRepository(conn),
            query_embedder=embedder,
            vector_index=vector_index,
            keyword_index=keyword_index,
            tokenizer=JiebaTokenizer(),
            **overrides,
        )

    yield SimpleNamespace(
        conn=conn,
        kb_id=insert_kb(conn, "检索知识库"),
        vector_index=vector_index,
        keyword_index=keyword_index,
        embedder=embedder,
        build_service=build_service,
    )
    conn.close()


def add_active_document(env, *, display_name, sha, items, deleted=False):
    """建立文档→版本→活动索引并写入切片、向量与关键词行

    :param items: [(内容, 向量, 预分词文本)]，子切片序号从 1 起
        （0 号为父切片，不参与召回）
    :param deleted: 置为软删除文档（指针链排除场景用）
    :return: (文档 ID, 索引 ID, 子切片 ID 列表)
    """
    doc_id = insert_document(
        env.conn,
        env.kb_id,
        sha,
        deleted_at=FIXED_TIME if deleted else None,
    )
    version_id = insert_version(env.conn, doc_id)
    index_id = insert_index_version(env.conn, version_id, status="active")
    collection = collection_name(index_id)
    namespace = fts_namespace_name(index_id)
    env.conn.execute(
        "UPDATE index_versions SET vector_collection = ?, fts_namespace = ?"
        " WHERE id = ?",
        (collection, namespace, index_id),
    )
    env.conn.execute(
        "UPDATE document_versions SET active_index_version_id = ? WHERE id = ?",
        (index_id, version_id),
    )
    env.conn.execute(
        "UPDATE documents SET active_document_version_id = ? WHERE id = ?",
        (version_id, doc_id),
    )

    parent_id = insert_chunk(env.conn, index_id, ordinal=0, content=display_name)
    chunk_ids: list[str] = []
    ids: list[str] = []
    vectors: list[list[float]] = []
    keyword_docs: list[KeywordDocument] = []
    for ordinal, (content, vector, tokenized) in enumerate(items, start=1):
        chunk_id = insert_chunk(
            env.conn, index_id, ordinal=ordinal, parent_chunk_id=parent_id,
            content=content,
        )
        chunk_ids.append(chunk_id)
        ids.append(chunk_id)
        vectors.append(list(vector))
        keyword_docs.append(KeywordDocument(chunk_id=chunk_id, content=tokenized))
    env.vector_index.upsert_vectors(collection, ids, vectors)
    env.keyword_index.rebuild_namespace(namespace, keyword_docs)
    return doc_id, index_id, chunk_ids


def test_dual_route_hits_fuse_with_dedup(retrieval_env):
    """双路命中去重为单候选，单路字段按实际命中填充"""
    env = retrieval_env
    _, _, chunk_ids = add_active_document(
        env,
        display_name="水果.pdf",
        sha="a" * 64,
        items=[
            ("苹果是一种水果", [1.0, 0.0, 0.0, 0.0], "苹果 是 一种 水果"),
            ("香蕉也是一种水果", [0.0, 1.0, 0.0, 0.0], "香蕉 也 是 一种 水果"),
        ],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]

    outcome = env.build_service().retrieve(env.kb_id, "苹果")

    assert outcome.dropped_hit_count == 0
    # 首切片双路命中融合居首；次切片仅向量路命中（相似度 0）随其后
    assert [candidate.chunk_id for candidate in outcome.candidates] == chunk_ids
    top = outcome.candidates[0]
    assert top.rrf_rank == 1
    assert top.vector_rank == 1
    assert top.vector_score == pytest.approx(1.0)
    assert top.keyword_rank == 1
    second = outcome.candidates[1]
    assert second.vector_rank == 2
    assert second.keyword_rank is None


def test_keyword_hit_elevates_candidate_over_vector_top(retrieval_env):
    """向量路第 1 的切片未命中关键词，关键词命中的切片经融合反超"""
    env = retrieval_env
    _, _, chunk_ids = add_active_document(
        env,
        display_name="双路.pdf",
        sha="b" * 64,
        items=[
            ("苹果相关内容", [1.0, 0.0, 0.0, 0.0], "苹果 相关 内容"),
            ("香蕉相关内容", [0.0, 1.0, 0.0, 0.0], "香蕉 相关 内容"),
        ],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]

    outcome = env.build_service().retrieve(env.kb_id, "香蕉")

    top = outcome.candidates[0]
    assert top.chunk_id == chunk_ids[1]
    assert top.vector_rank == 2
    assert top.keyword_rank == 1
    assert top.rrf_score == pytest.approx(1 / 62 + 1 / 61)


def test_missing_chunk_fact_drops_hit_and_counts(retrieval_env):
    """切片事实缺失的命中按漂移丢弃并计数，不进入融合"""
    env = retrieval_env
    _, _, chunk_ids = add_active_document(
        env,
        display_name="漂移.pdf",
        sha="c" * 64,
        items=[
            ("苹果内容", [1.0, 0.0, 0.0, 0.0], "苹果 内容"),
            ("香蕉内容", [0.0, 1.0, 0.0, 0.0], "香蕉 内容"),
        ],
    )
    env.conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_ids[0],))
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]

    outcome = env.build_service().retrieve(env.kb_id, "苹果")

    # 漂移切片的向量路与关键词路命中各丢弃一次
    assert outcome.dropped_hit_count == 2
    assert [candidate.chunk_id for candidate in outcome.candidates] == [chunk_ids[1]]


def test_soft_deleted_document_excluded_from_retrieval(retrieval_env):
    """软删文档不进入指针链，其索引内容不被检索"""
    env = retrieval_env
    _, _, active_chunks = add_active_document(
        env,
        display_name="在库.pdf",
        sha="d" * 64,
        items=[("在库内容", [1.0, 0.0, 0.0, 0.0], "在库 内容")],
    )
    add_active_document(
        env,
        display_name="已删.pdf",
        sha="e" * 64,
        items=[("苹果内容", [1.0, 0.0, 0.0, 0.0], "苹果 内容")],
        deleted=True,
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]

    outcome = env.build_service().retrieve(env.kb_id, "苹果")

    assert [candidate.chunk_id for candidate in outcome.candidates] == active_chunks


def test_staging_index_not_retrievable(retrieval_env):
    """staging 索引虽有资产名与双索引内容，未激活不参与检索"""
    env = retrieval_env
    _, _, active_chunks = add_active_document(
        env,
        display_name="正式.pdf",
        sha="f" * 64,
        items=[("正式内容", [1.0, 0.0, 0.0, 0.0], "正式 内容")],
    )
    doc_id = insert_document(env.conn, env.kb_id, "9" * 64)
    version_id = insert_version(env.conn, doc_id)
    staging_index = insert_index_version(env.conn, version_id, status="staging")
    env.conn.execute(
        "UPDATE index_versions SET vector_collection = ?, fts_namespace = ?"
        " WHERE id = ?",
        (
            collection_name(staging_index),
            fts_namespace_name(staging_index),
            staging_index,
        ),
    )
    staging_chunk = insert_chunk(
        env.conn, staging_index, ordinal=0, content="苹果 staging 内容"
    )
    env.vector_index.upsert_vectors(
        collection_name(staging_index), [staging_chunk], [[1.0, 0.0, 0.0, 0.0]]
    )
    env.keyword_index.rebuild_namespace(
        fts_namespace_name(staging_index),
        [KeywordDocument(chunk_id=staging_chunk, content="苹果 staging 内容")],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]

    outcome = env.build_service().retrieve(env.kb_id, "苹果")

    assert [candidate.chunk_id for candidate in outcome.candidates] == active_chunks


def test_kb_without_retrievable_indexes_returns_empty_without_embedding(retrieval_env):
    """无可检索索引返回空产出且不发起查询嵌入"""
    env = retrieval_env

    outcome = env.build_service().retrieve(env.kb_id, "苹果")

    assert outcome.candidates == ()
    assert outcome.dropped_hit_count == 0
    assert env.embedder.calls == []


def test_blank_question_rejected(retrieval_env):
    """空白问题被拒绝"""
    with pytest.raises(ValueError):
        retrieval_env.build_service().retrieve(retrieval_env.kb_id, "   ")


def test_config_overrides_shrink_candidates(retrieval_env):
    """注入小候选数：两路各自截断后融合产出收敛"""
    env = retrieval_env
    _, _, chunk_ids = add_active_document(
        env,
        display_name="截断.pdf",
        sha="8" * 64,
        items=[
            ("苹果", [1.0, 0.0, 0.0, 0.0], "苹果"),
            ("苹果 苹果", [0.0, 1.0, 0.0, 0.0], "苹果 苹果"),
            ("苹果 苹果 苹果", [0.0, 0.0, 1.0, 0.0], "苹果 苹果 苹果"),
        ],
    )
    env.embedder.vector = [1.0, 0.0, 0.0, 0.0]

    outcome = env.build_service(
        vector_top_k=1, keyword_top_k=1, fused_top_k=2
    ).retrieve(env.kb_id, "苹果")

    # 向量路截断取首切片；关键词路按词频取末切片；融合各 1/61 按序号排序
    assert [candidate.chunk_id for candidate in outcome.candidates] == [
        chunk_ids[0],
        chunk_ids[2],
    ]


def test_worker_pipeline_state_is_retrievable(env):
    """真实导入管线产物可检索：指针链、命名合同与双索引内容齐备"""
    task_id = import_file(env, "检索.txt", "苹果是一种水果".encode())
    worker = env.build()

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    indexes = env.repos["indexes"].list_by_document_version(
        task.document_version_id
    )
    active = [item for item in indexes if item.status.value == "active"]
    assert len(active) == 1

    service = RetrievalService(
        index_repo=env.repos["indexes"],
        chunk_repo=env.repos["chunks"],
        query_embedder=FixedQueryEmbedder([1.0, 1.0, 1.0, 1.0]),
        vector_index=env.vector_index,
        keyword_index=env.keyword_index,
        tokenizer=env.tokenizer,
    )

    outcome = service.retrieve(env.kb_id, "苹果")

    assert len(outcome.candidates) == 1
    top = outcome.candidates[0]
    assert top.document_version_id == task.document_version_id
    assert top.vector_rank == 1
    assert top.keyword_rank == 1
    assert top.rrf_score == pytest.approx(2 / 61)
