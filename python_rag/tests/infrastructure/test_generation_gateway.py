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
        status_code=200,
        output=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
        ),
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
    assert model == "qwen-plus"
    assert messages == [{"role": "user", "content": "问题"}]
    assert max_tokens == 99
    assert timeout == 7


def test_empty_delta_skipped_and_missing_choices_rejected():
    """空增量跳过；choices 缺失按协议违规拒绝"""
    broken = SimpleNamespace(status_code=200, output=SimpleNamespace(choices=[]))
    gateway = DashScopeGenerationGateway(
        api_key="sk-test", invoker=RecordingInvoker([broken])
    )

    with pytest.raises(GenerationProtocolViolationError):
        list(gateway.stream_answer([{"role": "user", "content": "问题"}]))


def test_transport_exception_is_transient():
    """调用抛异常统一归瞬态，消息只含异常类型名"""
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
        ("InvalidApiKey", GenerationAuthError),
        ("Unknown.Code", GenerationProtocolViolationError),
    ],
)
def test_business_code_error_mapping(code, expected):
    """业务错误码映射：限流族单列为查询合同错误码"""
    gateway = DashScopeGenerationGateway(
        api_key="sk-test",
        invoker=lambda *args: iter(
            [SimpleNamespace(status_code=400, output=None, code=code, message="错误")]
        ),
    )

    with pytest.raises(expected):
        list(gateway.stream_answer([{"role": "user", "content": "问题"}]))


def test_error_message_never_contains_api_key():
    """错误消息不携带密钥"""
    gateway = DashScopeGenerationGateway(
        api_key="sk-secret-key-value",
        invoker=lambda *args: iter(
            [
                SimpleNamespace(
                    status_code=400,
                    output=None,
                    code="InvalidApiKey",
                    message="密钥无效",
                )
            ]
        ),
    )

    with pytest.raises(GenerationAuthError) as exc_info:
        list(gateway.stream_answer([{"role": "user", "content": "问题"}]))

    assert "sk-secret-key-value" not in str(exc_info.value)
