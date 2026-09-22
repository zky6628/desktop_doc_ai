# -*- coding: utf-8 -*-
"""嵌入缓存仓储测试：序列化往返、按模型隔离与损坏行防御"""
import pytest

from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteEmbeddingCacheRepository,
)
from tests.infrastructure.schema_helpers import fresh_db


def _repo(tmp_path):
    db_path, _ = fresh_db(tmp_path, name="embed_cache.db")
    conn = connect(db_path)
    return conn, SQLiteEmbeddingCacheRepository(conn)


def test_put_and_get_roundtrip(tmp_path):
    """写入的向量可按哈希读回（float32 序列化保精度容差）"""
    conn, repo = _repo(tmp_path)
    try:
        vector = [0.5, -1.25, 3.75, 0.0]
        repo.put_many("text-embedding-v4", "document", 4, [("hash-a", vector)])

        hits = repo.get_many(
            "text-embedding-v4", "document", ["hash-a", "hash-miss"]
        )

        assert set(hits) == {"hash-a"}
        assert hits["hash-a"] == pytest.approx(vector, abs=1e-6)
    finally:
        conn.close()


def test_cache_isolated_by_model(tmp_path):
    """同内容哈希在不同模型名下互不可见"""
    conn, repo = _repo(tmp_path)
    try:
        repo.put_many("model-a", "document", 2, [("hash-x", [1.0, 2.0])])

        assert repo.get_many("model-b", "document", ["hash-x"]) == {}
        assert "hash-x" in repo.get_many("model-a", "document", ["hash-x"])
    finally:
        conn.close()


def test_put_many_is_idempotent(tmp_path):
    """同键重复写入保留既有值（不覆盖）"""
    conn, repo = _repo(tmp_path)
    try:
        repo.put_many("m", "document", 2, [("h", [1.0, 1.0])])
        repo.put_many("m", "document", 2, [("h", [9.0, 9.0])])

        assert repo.get_many("m", "document", ["h"])["h"] == pytest.approx([1.0, 1.0])
    finally:
        conn.close()


def test_corrupted_row_treated_as_miss(tmp_path):
    """序列化长度与登记维度不符的损坏行按未命中处理"""
    conn, repo = _repo(tmp_path)
    try:
        repo.put_many("m", "document", 4, [("good", [1.0, 2.0, 3.0, 4.0])])
        # 直接写损坏行：blob 长度与维度不符
        conn.execute(
            "INSERT INTO embedding_cache"
            " (model_name, text_type, content_hash, vector, dimensions, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("m", "document", "bad", b"\x00\x00\x00", 4, "2026-01-01T00:00:00+00:00"),
        )

        hits = repo.get_many("m", "document", ["good", "bad"])

        assert set(hits) == {"good"}
    finally:
        conn.close()


def test_empty_inputs_short_circuit(tmp_path):
    """空输入直接返回空，不发起查询"""
    conn, repo = _repo(tmp_path)
    try:
        assert repo.get_many("m", "document", []) == {}
        repo.put_many("m", "document", 2, [])
    finally:
        conn.close()
