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

