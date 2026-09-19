# -*- coding: utf-8 -*-
"""Chroma 向量索引适配器测试：集合隔离、幂等写入与缺失集合零值

chromadb 的内存客户端按相同配置在进程内共享实例，因此每个用例使用
唯一集合名，保证用例间互不干扰
"""
import uuid

import chromadb
import pytest

from app.infrastructure.vectorindex import ChromaVectorIndexAdapter


@pytest.fixture()
def adapter():
    return ChromaVectorIndexAdapter(chromadb.EphemeralClient())


def _collection() -> str:
    """当前用例专属的集合名（进程内共享内存库下的隔离手段）"""
    return f"wb-idx-{uuid.uuid4()}"


def test_upsert_and_read_back_roundtrip(adapter):
    """写入向量后数量与 ID 集合可完整读回"""
    name = _collection()
    adapter.upsert_vectors(name, ["c1", "c2"], [[1.0, 2.0], [3.0, 4.0]])

    assert adapter.count_vectors(name) == 2
    assert set(adapter.list_vector_ids(name)) == {"c1", "c2"}


def test_reupsert_same_ids_is_idempotent(adapter):
    """同 ID 重复写入不产生重复记录"""
    name = _collection()
    adapter.upsert_vectors(name, ["c1"], [[1.0, 2.0]])
    adapter.upsert_vectors(name, ["c1"], [[9.0, 9.0]])

    assert adapter.count_vectors(name) == 1


def test_collections_are_isolated_by_name(adapter):
    """不同集合互相隔离：写入 A 不影响 B 的读取"""
    name_a = _collection()
    name_b = _collection()
    adapter.upsert_vectors(name_a, ["c1"], [[1.0, 2.0]])

    assert adapter.count_vectors(name_b) == 0


def test_missing_collection_reads_as_zero(adapter):
    """不存在的集合读取为零值且不产生创建副作用"""
    name = _collection()
    assert adapter.count_vectors(name) == 0
    assert adapter.list_vector_ids(name) == []


def test_delete_collection_removes_records(adapter):
    """删除集合后读取归零；重复删除为无操作"""
    name = _collection()
    adapter.upsert_vectors(name, ["c1"], [[1.0, 2.0]])

    adapter.delete_collection(name)
    adapter.delete_collection(name)

    assert adapter.count_vectors(name) == 0


def test_delete_missing_collection_is_noop(adapter):
    """删除不存在的集合不抛异常"""
    adapter.delete_collection(_collection())


def test_created_collections_use_cosine_space():
    """写入创建的集合固定为余弦空间（查询距离即余弦距离的基准）"""
    client = chromadb.EphemeralClient()
    adapter = ChromaVectorIndexAdapter(client)
    name = _collection()
    adapter.upsert_vectors(name, ["c1"], [[1.0, 0.0]])

    metadata = client.get_collection(name).metadata or {}
    assert metadata.get("hnsw:space") == "cosine"


def test_query_vectors_orders_by_relevance_with_normalized_score(adapter):
    """查询按余弦相似度排序：score = 1 - 原始距离且原始距离保留"""
    name = _collection()
    adapter.upsert_vectors(
        name,
        ["c-near", "c-far"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )

    hits = adapter.query_vectors(name, [1.0, 0.0, 0.0], top_k=2)

    assert hits[0].chunk_id == "c-near"
    assert hits[0].raw_score == pytest.approx(0.0, abs=1e-6)
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)
    assert hits[1].chunk_id == "c-far"
    assert hits[1].score == pytest.approx(1.0 - hits[1].raw_score)


def test_query_vectors_respects_top_k(adapter):
    """查询返回数量不超过 top_k 且相关度降序"""
    name = _collection()
    adapter.upsert_vectors(
        name,
        ["c1", "c2", "c3"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    )

    hits = adapter.query_vectors(name, [1.0, 0.0, 0.0], top_k=2)

    assert len(hits) == 2
    assert hits[0].chunk_id == "c1"


def test_query_vectors_missing_collection_returns_empty_without_creating(adapter):
    """查询不存在的集合返回空列表且不产生创建副作用"""
    name = _collection()

    assert adapter.query_vectors(name, [1.0, 0.0], top_k=5) == []
    assert name not in adapter.list_collections()

