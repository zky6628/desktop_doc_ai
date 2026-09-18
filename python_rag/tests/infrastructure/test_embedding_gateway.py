# -*- coding: utf-8 -*-
"""Embedding 网关测试：批量对齐、错误分类与密钥脱敏（零网络）"""
from types import SimpleNamespace

import pytest

from app.domain import embedding
from app.domain.errors import (
    EmbeddingAuthError,
    EmbeddingError,
    EmbeddingProtocolViolationError,
    EmbeddingQuotaError,
    EmbeddingTransientError,
)
from app.infrastructure.embedding import DashScopeEmbeddingGateway

# 密钥样例：用于断言任何错误消息都不泄露密钥
_SECRET_KEY = "sk-test-secret-key-000111"


class _FakeInvoker:
    """供应方调用替身：记录调用参数，按队列返回响应或抛出异常"""

    def __init__(self, results) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, list[str], int, str]] = []

    def __call__(self, model, input_texts, dimensions, text_type, api_key):
        self.calls.append((model, list(input_texts), dimensions, text_type))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _ok_response(
    batch_size: int,
    *,
    dimensions: int = embedding.EMBEDDING_DIMENSIONS,
    reverse: bool = False,
):
    """构造成功响应：向量以序号为值便于断言对齐，可选乱序回传"""
    items = [
        {"text_index": index, "embedding": [float(index + 1)] * dimensions}
        for index in range(batch_size)
    ]
    if reverse:
        items.reverse()
    return SimpleNamespace(
        status_code=200, output={"embeddings": items}, code=None, message=None
    )


def _error_response(code: str | None, status_code: int):
    return SimpleNamespace(
        status_code=status_code, output=None, code=code, message="供应方说明文本"
    )


def _gateway(invoker) -> DashScopeEmbeddingGateway:
    return DashScopeEmbeddingGateway(api_key=_SECRET_KEY, invoker=invoker)


def test_single_batch_aligned_in_input_order():
    """单批调用：乱序 text_index 响应被排序对齐，向量按输入顺序返回"""
    texts = ["甲", "乙", "丙"]
    invoker = _FakeInvoker([_ok_response(3, reverse=True)])
    gateway = _gateway(invoker)

    vectors = gateway.embed_texts(texts)

    assert vectors == [[1.0] * 1024, [2.0] * 1024, [3.0] * 1024]
    assert invoker.calls == [
        (embedding.EMBEDDING_MODEL, texts, embedding.EMBEDDING_DIMENSIONS, "document")
    ]


def test_multi_batch_preserves_cross_batch_order():
    """超批上限分批调用：请求按序切片，结果跨批保持输入顺序"""
    texts = [f"文本{index}" for index in range(13)]
    invoker = _FakeInvoker([_ok_response(10), _ok_response(3)])
    gateway = _gateway(invoker)

    vectors = gateway.embed_texts(texts)

    assert [len(batch) for _, batch, _, _ in invoker.calls] == [10, 3]
    assert invoker.calls[0][1] == texts[:10]
    assert invoker.calls[1][1] == texts[10:]
    assert len(vectors) == 13
    # 前批向量在前：前 10 条为批内序号向量，后 3 条为下批序号向量
    assert vectors[9] == [10.0] * 1024
    assert vectors[10] == [1.0] * 1024


def test_empty_input_issues_no_request():
    """空输入返回空列表且不发起任何请求"""
    invoker = _FakeInvoker([])
    gateway = _gateway(invoker)

    assert gateway.embed_texts([]) == []
    assert invoker.calls == []


def test_dimension_mismatch_rejected_as_protocol_violation():
    """响应向量维度与配置不一致按协议违规拒绝"""
    invoker = _FakeInvoker([_ok_response(2, dimensions=512)])
    gateway = _gateway(invoker)

    with pytest.raises(EmbeddingProtocolViolationError):
        gateway.embed_texts(["甲", "乙"])


def test_count_mismatch_rejected_as_protocol_violation():
    """响应向量数量与输入不一致按协议违规拒绝"""
    invoker = _FakeInvoker([_ok_response(3)])
    gateway = _gateway(invoker)

    with pytest.raises(EmbeddingProtocolViolationError):
        gateway.embed_texts(["甲", "乙"])


def test_missing_or_duplicated_index_rejected():
    """缺少文本序号或序号不连续均按协议违规拒绝"""
    broken = SimpleNamespace(
        status_code=200,
        output={"embeddings": [
            {"text_index": 0, "embedding": [1.0] * 1024},
            {"embedding": [2.0] * 1024},
        ]},
        code=None,
        message=None,
    )
    duplicated = SimpleNamespace(
        status_code=200,
        output={"embeddings": [
            {"text_index": 0, "embedding": [1.0] * 1024},
            {"text_index": 0, "embedding": [2.0] * 1024},
        ]},
        code=None,
        message=None,
    )

    with pytest.raises(EmbeddingProtocolViolationError):
        _gateway(_FakeInvoker([broken])).embed_texts(["甲", "乙"])
    with pytest.raises(EmbeddingProtocolViolationError):
        _gateway(_FakeInvoker([duplicated])).embed_texts(["甲", "乙"])


def test_rate_limiting_code_maps_to_transient():
    """限流族业务码（不含配额后缀）映射为瞬态可重试错误"""
    invoker = _FakeInvoker([_error_response("Throttling", 429)])
    gateway = _gateway(invoker)

    with pytest.raises(EmbeddingTransientError):
        gateway.embed_texts(["甲"])


def test_allocation_quota_code_maps_to_quota():
    """携带配额后缀的限流码与欠费码映射为配额错误"""
    for code in ("Throttling.AllocationQuota", "Arrearage"):
        invoker = _FakeInvoker([_error_response(code, 429)])
        with pytest.raises(EmbeddingQuotaError):
            _gateway(invoker).embed_texts(["甲"])


def test_invalid_key_code_maps_to_auth():
    """认证类业务码映射为认证错误"""
    invoker = _FakeInvoker([_error_response("InvalidApiKey", 401)])
    gateway = _gateway(invoker)

    with pytest.raises(EmbeddingAuthError):
        gateway.embed_texts(["甲"])


def test_unknown_business_code_maps_to_protocol_violation():
    """未知业务错误码按协议违规处理（与云端解析适配器口径一致）"""
    invoker = _FakeInvoker([_error_response("MysteryCode", 400)])
    gateway = _gateway(invoker)

    with pytest.raises(EmbeddingProtocolViolationError):
        gateway.embed_texts(["甲"])


def test_no_code_server_error_maps_to_transient():
    """无业务码的 5xx 响应按传输层瞬态处理"""
    invoker = _FakeInvoker([_error_response(None, 503)])
    gateway = _gateway(invoker)

    with pytest.raises(EmbeddingTransientError):
        gateway.embed_texts(["甲"])


def test_invoker_exception_maps_to_transient():
    """调用抛出异常即传输层瞬态（供应方业务错误不会以异常表达）"""
    invoker = _FakeInvoker([ConnectionError("network down")])
    gateway = _gateway(invoker)

    with pytest.raises(EmbeddingTransientError):
        gateway.embed_texts(["甲"])


def test_error_messages_never_contain_api_key():
    """全部错误路径的消息不携带密钥"""
    error_responses = [
        _error_response("InvalidApiKey", 401),
        _error_response(None, 503),
        _ok_response(1, dimensions=8),
    ]
    for response in error_responses:
        gateway = _gateway(_FakeInvoker([response]))
        with pytest.raises(EmbeddingError) as exc_info:
            gateway.embed_texts(["甲"])
        assert _SECRET_KEY not in str(exc_info.value)

    with pytest.raises(EmbeddingError) as exc_info:
        _gateway(_FakeInvoker([ConnectionError("network down")])).embed_texts(["甲"])
    assert _SECRET_KEY not in str(exc_info.value)
