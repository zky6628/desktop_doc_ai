# -*- coding: utf-8 -*-
"""查询侧向量化缓存装饰器：同问题命中缓存不发起供应方调用

键为模型 + 查询侧别 + 问题内容哈希：评测编排同一问题集在多组切片
参数下重复执行、线上重复提问均直接复用既有向量，不重复调用嵌入
API。缓存与网关产出同源（模型与维度由装配方按领域常量注入）；
未命中即调网关并回填，嵌入调用失败不写缓存（下次查询重新计算）。
"""
import hashlib

from app.domain import embedding
from app.domain.ports import (
    EmbeddingCacheRepository,
    QueryEmbeddingGateway,
)


class CachingQueryEmbedder(QueryEmbeddingGateway):
    """带缓存的查询侧向量化网关

    :param gateway: 真实供应方网关（缓存未命中时调用）
    :param cache: 嵌入缓存仓储
    :param model_name: 产生向量的嵌入模型（缓存键一部分）
    :param dimensions: 向量维度（缓存写入登记用）
    """

    def __init__(
        self,
        *,
        gateway: QueryEmbeddingGateway,
        cache: EmbeddingCacheRepository,
        model_name: str = embedding.EMBEDDING_MODEL,
        dimensions: int = embedding.EMBEDDING_DIMENSIONS,
    ) -> None:
        self._gateway = gateway
        self._cache = cache
        self._model_name = model_name
        self._dimensions = dimensions

    def embed_query(self, question: str) -> list[float]:
        """先查缓存，未命中调网关并回填（方法契约见领域 Port 定义）"""
        content_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
        cached = self._cache.get_many(
            self._model_name, embedding.EMBEDDING_QUERY_TEXT_TYPE, [content_hash]
        )
        vector = cached.get(content_hash)
        if vector is not None:
            return vector
        vector = self._gateway.embed_query(question)
        self._cache.put_many(
            self._model_name,
            embedding.EMBEDDING_QUERY_TEXT_TYPE,
            self._dimensions,
            [(content_hash, vector)],
        )
        return vector
