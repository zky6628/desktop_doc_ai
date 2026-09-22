# -*- coding: utf-8 -*-
"""Embedding 适配器：文本向量化网关（文档侧）与查询侧缓存装饰

DTO 与原始响应不越出本包；上层只接触领域 Port 与领域错误。
"""
from .caching_query_embedder import CachingQueryEmbedder
from .dashscope_gateway import DashScopeEmbeddingGateway

__all__ = ["CachingQueryEmbedder", "DashScopeEmbeddingGateway"]
