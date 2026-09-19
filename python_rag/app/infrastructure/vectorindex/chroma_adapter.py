# -*- coding: utf-8 -*-
"""Chroma 向量索引适配器：按索引版本隔离的集合写入与验证

集合按索引版本隔离，命名合同由调用方传入（工作台侧为
"wb-idx-{索引版本 ID}"——归属信息由名字唯一承载，不再写 metadata）。
写入按记录 ID 幂等 upsert，集合按需创建；读取侧对不存在的集合
返回零值而非创建，避免验证与清理路径产生空集合副作用。检索查询
属检索里程碑能力，本适配器只承载写入与验证。
"""
from collections.abc import Sequence
from typing import Any

from app.domain.ports import VectorIndexGateway as VectorIndexGatewayPort


class ChromaVectorIndexAdapter(VectorIndexGatewayPort):
    """Chroma 持久化向量库适配器

    :param client: Chroma 客户端（装配层给 PersistentClient，
        测试可注入 EphemeralClient 等实现）
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def upsert_vectors(
        self,
        collection_name: str,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        """按记录 ID 幂等写入/更新向量（方法契约见领域 Port 定义）"""
        collection = self._client.get_or_create_collection(name=collection_name)
        collection.upsert(ids=list(ids), embeddings=[list(v) for v in vectors])

    def count_vectors(self, collection_name: str) -> int:
        """返回集合内向量数量；集合不存在返回 0"""
        collection = self._try_get_collection(collection_name)
        return collection.count() if collection is not None else 0

    def list_vector_ids(self, collection_name: str) -> list[str]:
        """返回集合内全部记录 ID；集合不存在返回空列表"""
        collection = self._try_get_collection(collection_name)
        if collection is None:
            return []
        return list(collection.get()["ids"])

    def delete_collection(self, collection_name: str) -> None:
        """删除整个集合；集合不存在时无操作"""
        collection = self._try_get_collection(collection_name)
        if collection is not None:
            self._client.delete_collection(collection_name)

    def _try_get_collection(self, collection_name: str) -> Any | None:
        """读取既有集合；不存在返回 None（不产生创建副作用）"""
        try:
            return self._client.get_collection(collection_name)
        except Exception:  # noqa: BLE001 - 供应方对缺失集合的异常类型跨版本不稳定，统一按不存在处理
            return None
