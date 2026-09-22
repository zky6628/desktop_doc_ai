# -*- coding: utf-8 -*-
"""DashScope 生成网关测试：零网络替身注入、流式增量与错误分类"""
from types import SimpleNamespace

import pytest

from app.domain.errors import (
    GenerationAuthError,
    GenerationProtocolViolationError,
    GenerationQuotaError,
    GenerationRateLimitedError,
    GenerationTransientError,
)
from app.infrastructure.generation import DashScopeGenerationGateway


class FakeHTTPError(Exception):
    """模拟 openai SDK 的 HTTP 状态错误（携带状态码与响应体）"""

    def __init__(self, status_code: int, body) -> None:
        super().__init__("http error")
        self.status_code = status_code
        self.body = body


class RecordingInvoker:
    """返回预置响应序列的替身（校验注入参数）"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, model, messages, max_tokens, request_timeout, api_key):
        self.calls.append((model, list(messages), max_tokens, request_timeout))
        yield from self.responses


def _delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
    )


def _usage_chunk(prompt_tokens: int, completion_tokens: int) -> SimpleNamespace:
    """用量专用收尾块（无 choices）"""
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def test_stream_answer_yields_deltas_in_order():
    """增量文本按供应方顺序产出，拼接即全文"""
    gateway = DashScopeGenerationGateway(
        api_key="sk-test",
        invoker=RecordingInvoker([_delta("根据"), _delta("[S1]，"), _delta("年假五天")]),
    )

    text = "".join(gateway.stream_answer([{"role": "user", "content": "问题"}]))

    assert text == "根据[S1]，年假五天"


def test_stream_answer_passes_config_to_supplier():
    """模型/消息/输出上限/超时按配置传给供应方"""
    invoker = RecordingInvoker([_delta("x")])
    gateway = DashScopeGenerationGateway(
        api_key="sk-test", invoker=invoker, max_tokens=99, request_timeout=7
    )

    list(gateway.stream_answer([{"role": "user", "content": "问题"}]))

    model, messages, max_tokens, timeout = invoker.calls[0]
    assert model == "qwen3.8-max"
    assert messages == [{"role": "user", "content": "问题"}]
    assert max_tokens == 99
    assert timeout == 7


def test_usage_chunk_captured_and_empty_choices_skipped():
    """用量收尾块（无 choices）跳过不产出文本，末次用量随流捕获"""
    gateway = DashScopeGenerationGateway(
        api_key="sk-test",
        invoker=RecordingInvoker([_delta("答案"), _usage_chunk(64, 39)]),
    )

    text = "".join(gateway.stream_answer([{"role": "user", "content": "问题"}]))

    assert text == "答案"
    assert gateway.last_usage == (64, 39)


def test_missing_choices_shape_rejected():
    """choices 形状缺失按协议违规拒绝"""
    broken = SimpleNamespace(choices=None)
    gateway = DashScopeGenerationGateway(
        api_key="sk-test", invoker=RecordingInvoker([broken])
    )

    with pytest.raises(GenerationProtocolViolationError):
        list(gateway.stream_answer([{"role": "user", "content": "问题"}]))


def test_transport_exception_is_transient():
    """连接层异常统一归瞬态，消息只含异常类型名"""
    gateway = DashScopeGenerationGateway(
        api_key="sk-test",
        invoker=lambda *args: (_ for _ in ()).throw(ConnectionError("reset")),
    )

    with pytest.raises(GenerationTransientError) as exc_info:
        list(gateway.stream_answer([{"role": "user", "content": "问题"}]))

    assert "ConnectionError" in str(exc_info.value)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("Throttling", GenerationRateLimitedError),
        ("SystemFlowControl", GenerationRateLimitedError),
        ("Arrearage", GenerationQuotaError),
        ("AllocationQuota.FreeTierOnly", GenerationQuotaError),
        ("InvalidApiKey", GenerationAuthError),
        ("Unknown.Code", GenerationProtocolViolationError),
    ],
)
def test_business_code_error_mapping(code, expected):
    """携带状态码的 HTTP 错误按响应体业务码映射：限流族单列为查询合同错误码"""
    gateway = DashScopeGenerationGateway(
        api_key="sk-test",
        invoker=lambda *args: (_ for _ in ()).throw(
            FakeHTTPError(400, {"error": {"code": code, "message": "错误"}})
        ),
    )

    with pytest.raises(expected):
        list(gateway.stream_answer([{"role": "user", "content": "问题"}]))


def test_status_without_body_code_falls_back_to_status_family():
    """无业务码时按 HTTP 状态分层（401 归认证）"""
    gateway = DashScopeGenerationGateway(
        api_key="sk-test",
        invoker=lambda *args: (_ for _ in ()).throw(FakeHTTPError(401, None)),
    )

    with pytest.raises(GenerationAuthError):
        list(gateway.stream_answer([{"role": "user", "content": "问题"}]))


def test_error_message_never_contains_api_key():
    """错误消息不携带密钥"""
    gateway = DashScopeGenerationGateway(
        api_key="sk-secret-key-value",
        invoker=lambda *args: (_ for _ in ()).throw(
            FakeHTTPError(400, {"error": {"code": "InvalidApiKey", "message": "密钥无效"}})
        ),
    )

    with pytest.raises(GenerationAuthError) as exc_info:
        list(gateway.stream_answer([{"role": "user", "content": "问题"}]))

    assert "sk-secret-key-value" not in str(exc_info.value)
