# -*- coding: utf-8 -*-
"""嵌入缓存仓储的 SQLite 实现：向量按 float32 小端序列化存储

缓存是全局正向资产（同内容任意索引版本可复用），不做失效与清理；
键含模型名，换模型后旧键自然不再命中。维度列供读取端完整性校验
（序列化长度与维度不符即视为未命中，不信任损坏行）。
"""
import struct
from collections.abc import Sequence

from app.domain.clock import utc_now_iso
from app.domain.ports import EmbeddingCacheRepository as EmbeddingCacheRepositoryPort

_FLOAT32 = struct.Struct("<f")


class SQLiteEmbeddingCacheRepository(EmbeddingCacheRepositoryPort):
    """embedding_cache 表的读写实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def get_many(
        self, model_name: str, text_type: str, content_hashes: Sequence[str]
    ) -> dict[str, list[float]]:
        if not content_hashes:
            return {}
        unique = sorted(set(content_hashes))
        placeholders = ",".join("?" * len(unique))
        rows = self._conn.execute(
            "SELECT content_hash, vector, dimensions FROM embedding_cache"
            " WHERE model_name = ? AND text_type = ?"
            f" AND content_hash IN ({placeholders})",
            (model_name, text_type, *unique),
        ).fetchall()
        hits: dict[str, list[float]] = {}
        for content_hash, blob, dimensions in rows:
            vector = _unpack_vector(blob)
            # 序列化长度与登记维度不符属损坏行，按未命中处理
            if vector is not None and len(vector) == dimensions:
                hits[content_hash] = vector
        return hits

    def put_many(
        self,
        model_name: str,
        text_type: str,
        dimensions: int,
        entries: Sequence[tuple[str, list[float]]],
    ) -> None:
        if not entries:
            return
        self._conn.executemany(
            "INSERT INTO embedding_cache"
            " (model_name, text_type, content_hash, vector, dimensions, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(model_name, text_type, content_hash) DO NOTHING",
            [
                (
                    model_name,
                    text_type,
                    content_hash,
                    _pack_vector(vector),
                    dimensions,
                    utc_now_iso(),
                )
                for content_hash, vector in entries
            ],
        )


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float] | None:
    """反序列化向量；字节数非 4 的整数倍视为损坏返回 None"""
    if not blob or len(blob) % _FLOAT32.size != 0:
        return None
    count = len(blob) // _FLOAT32.size
    return list(struct.unpack(f"<{count}f", blob))
