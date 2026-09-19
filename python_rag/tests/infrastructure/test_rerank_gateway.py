# -*- coding: utf-8 -*-
"""DashScope 重排网关测试：零网络替身注入、结果校验与错误分类

响应替身模拟 SDK ReRankResponse 的结构（status_code/output/code/
message），结果条目模拟 ReRankResult（index/relevance_score）。
"""
from types import SimpleNamespace

import pytest

from app.domain.errors import (
    RerankAuthError,
    RerankProtocolViolationError,
    RerankQuotaError,
    RerankTransientError,
)
from app.infrastructure.rerank import DashScopeRerankGateway


class RecordingInvoker:
    """记录调用参数的替身（校验注入参数与调用次数）"""

    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[tuple] = []

    def __call__(self, model, query, documents, top_n, request_timeout, api_key):
        self.calls.append((model, query, list(documents), top_n, request_timeout))
        return self.response


def _response(status_code=200, results=None, code=None, message=""):
    output = None if results is None else SimpleNamespace(results=results)
    return SimpleNamespace(
        status_code=status_code, output=output, code=code, message=message
    )


def _result(index, score):
    return SimpleNamespace(index=index, relevance_score=score, document=None)


def _gateway(response, **overrides):
    invoker = RecordingInvoker(response)
    gateway = DashScopeRerankGateway(
        api_key="sk-test", invoker=invoker, **overrides
    )
    return gateway, invoker


def test_rerank_orders_by_score_with_stable_tie():
    """结果按相关度降序返回，同分按输入下标升序稳定排序"""
    gateway, _ = _gateway(
        _response(results=[_result(2, 0.5), _result(0, 0.9), _result(1, 0.9)])
    )

    hits = gateway.rerank("问题", ["甲", "乙", "丙"])

    assert [(hit.index, hit.score) for hit in hits] == [(0, 0.9), (1, 0.9), (2, 0.5)]


def test_rerank_passes_config_to_supplier():
    """模型/问题/文档/输出数量/超时按配置传给供应方"""
    gateway, invoker = _gateway(
        _response(results=[_result(0, 0.8), _result(2, 0.6)]),
        top_n=2,
        request_timeout=7,
    )

    gateway.rerank("公司年假", ["文本一", "文本二", "文本三"])

    model, query, documents, top_n, timeout = invoker.calls[0]
    assert model == "qwen3-rerank"
    assert query == "公司年假"
    assert documents == ["文本一", "文本二", "文本三"]
    assert top_n == 2
    assert timeout == 7


def test_empty_documents_returns_empty_without_invoke():
    """空文档序列不发起请求，直接返回空列表"""
    gateway, invoker = _gateway(_response())

    assert gateway.rerank("问题", []) == []
    assert invoker.calls == []


def test_result_count_mismatch_rejected():
    """结果数量与 min(top_n, 文档数) 不符按协议违规拒绝"""
    gateway, _ = _gateway(
        _response(
            results=[_result(0, 0.9), _result(1, 0.8), _result(2, 0.7)]
        ),
        top_n=2,
    )

    with pytest.raises(RerankProtocolViolationError):
        gateway.rerank("问题", ["甲", "乙", "丙"])


def test_index_out_of_range_rejected():
    """下标越界按协议违规拒绝"""
    gateway, _ = _gateway(
        _response(results=[_result(0, 0.9), _result(3, 0.8), _result(1, 0.7)])
    )

    with pytest.raises(RerankProtocolViolationError):
        gateway.rerank("问题", ["甲", "乙", "丙"])


def test_duplicate_index_rejected():
    """下标重复按协议违规拒绝"""
    gateway, _ = _gateway(
        _response(results=[_result(0, 0.9), _result(0, 0.8), _result(1, 0.7)])
    )

    with pytest.raises(RerankProtocolViolationError):
        gateway.rerank("问题", ["甲", "乙", "丙"])


def test_non_numeric_score_rejected():
    """分数类型非法按协议违规拒绝"""
    gateway, _ = _gateway(
        _response(results=[_result(0, "0.9"), _result(1, 0.8), _result(2, 0.7)])
    )

    with pytest.raises(RerankProtocolViolationError):
        gateway.rerank("问题", ["甲", "乙", "丙"])


def test_missing_index_rejected():
    """结果缺少下标按协议违规拒绝"""
    broken = SimpleNamespace(relevance_score=0.9)
    gateway, _ = _gateway(
        _response(results=[broken, _result(1, 0.8), _result(2, 0.7)])
    )

    with pytest.raises(RerankProtocolViolationError):
        gateway.rerank("问题", ["甲", "乙", "丙"])


def test_transport_exception_is_transient():
    """调用抛异常统一归瞬态，消息只含异常类型名"""
    gateway = DashScopeRerankGateway(
        api_key="sk-test",
        invoker=lambda *args: (_ for _ in ()).throw(ConnectionError("reset")),
    )

    with pytest.raises(RerankTransientError) as exc_info:
        gateway.rerank("问题", ["甲"])

    assert "ConnectionError" in str(exc_info.value)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("Throttling", RerankTransientError),
        ("SystemFlowControl", RerankTransientError),
        ("Throttling.AllocationQuota", RerankQuotaError),
        ("Arrearage", RerankQuotaError),
        ("InvalidApiKey", RerankAuthError),
        ("AccessDenied", RerankAuthError),
        ("Unknown.Code", RerankProtocolViolationError),
    ],
)
def test_business_code_error_mapping(code, expected):
    """业务错误码经共享口径映射到领域错误族"""
    gateway, _ = _gateway(
        _response(status_code=400, code=code, message="供应商错误")
    )

    with pytest.raises(expected):
        gateway.rerank("问题", ["甲", "乙", "丙"])


def test_status_without_code_layering():
    """无业务码时按 HTTP 状态分层：401/403 认证、5xx 瞬态、其余协议违规"""
    auth_gateway, _ = _gateway(_response(status_code=401))
    with pytest.raises(RerankAuthError):
        auth_gateway.rerank("问题", ["甲", "乙", "丙"])

    transient_gateway, _ = _gateway(_response(status_code=502))
    with pytest.raises(RerankTransientError):
        transient_gateway.rerank("问题", ["甲", "乙", "丙"])

    protocol_gateway, _ = _gateway(_response(status_code=418))
    with pytest.raises(RerankProtocolViolationError):
        protocol_gateway.rerank("问题", ["甲", "乙", "丙"])


def test_error_message_never_contains_api_key():
    """错误消息不携带密钥"""
    gateway = DashScopeRerankGateway(
        api_key="sk-secret-key-value",
        invoker=lambda *args: SimpleNamespace(
            status_code=400,
            output=None,
            code="InvalidApiKey",
            message="密钥无效",
        ),
    )

    with pytest.raises(RerankAuthError) as exc_info:
        gateway.rerank("问题", ["甲", "乙", "丙"])

    assert "sk-secret-key-value" not in str(exc_info.value)
