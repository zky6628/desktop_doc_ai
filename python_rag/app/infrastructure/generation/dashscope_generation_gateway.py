# -*- coding: utf-8 -*-
"""DashScope 生成适配器：qwen-plus 流式调用与错误分类

stream=True 时供应方返回增量响应生成器，适配器逐段产出增量文本；
响应结构按协议校验（choices/message 增量字段缺失即协议违规）。
错误分类边界与向量化/重排网关一致：调用抛异常即传输层瞬态，限流
族业务码单列（查询合同有独立错误码），认证/配额归服务端配置问题，
未知业务码按协议违规。请求超时经 request_timeout 注入。密钥只经
构造器注入，不进入日志与错误消息。
"""
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from dashscope import Generation

from app.domain import generation
from app.domain.errors import (
    GenerationAuthError,
    GenerationError,
    GenerationProtocolViolationError,
    GenerationQuotaError,
    GenerationRateLimitedError,
    GenerationTransientError,
)
from app.domain.ports import GenerationGateway as GenerationGatewayPort

from ..dashscope_support import ErrorFamily, resolve_code_error

# 本网关的领域错误族：限流单列（查询合同独立错误码），其余经共享口径
_GENERATION_ERROR_FAMILY = ErrorFamily(
    auth=GenerationAuthError,
    quota=GenerationQuotaError,
    transient=GenerationTransientError,
    protocol=GenerationProtocolViolationError,
)


def _invoke_sdk(
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    request_timeout: int,
    api_key: str,
) -> Any:
    """DashScope SDK 的默认调用绑定（测试注入替身替换）"""
    # 供应方 SDK 运行时接受同构 dict 消息（类型标注为 Message 对象）
    return Generation.call(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        result_format="message",
        max_tokens=max_tokens,
        stream=True,
        incremental_output=True,
        request_timeout=request_timeout,
        api_key=api_key,
    )


class DashScopeGenerationGateway(GenerationGatewayPort):
    """DashScope 流式生成网关

    :param api_key: API Key（构造器注入，绝不写日志与消息）
    :param invoker: 供应方调用函数（缺省绑定 SDK，测试注入替身）
    :param model: 生成模型名
    :param max_tokens: 输出 token 上限
    :param request_timeout: 单次流式请求超时（秒，约束整条流）
    """

    def __init__(
        self,
        *,
        api_key: str,
        invoker: Callable[[str, list[dict[str, str]], int, int, str], Any]
        | None = None,
        model: str = generation.GENERATION_MODEL,
        max_tokens: int = generation.GENERATION_MAX_OUTPUT_TOKENS,
        request_timeout: int = generation.GENERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._invoke = invoker if invoker is not None else _invoke_sdk
        self._model = model
        self._max_tokens = max_tokens
        self._request_timeout = request_timeout
        self._last_usage: tuple[int, int] | None = None

    @property
    def last_usage(self) -> tuple[int, int] | None:
        """最近一次流式生成的供应方用量 (输入, 输出) token（无则 None）"""
        return self._last_usage

    def stream_answer(
        self, messages: Sequence[dict[str, str]]
    ) -> Iterator[str]:
        """按消息序列流式生成回答（方法契约见领域 Port 定义）"""
        self._last_usage = None
        try:
            response_stream = self._invoke(
                self._model,
                list(messages),
                self._max_tokens,
                self._request_timeout,
                self._api_key,
            )
            return self._iterate(response_stream)
        except GenerationError:
            raise
        except Exception as exc:
            # 调用边界兜底：SDK 传输层失败以异常表达，统一归瞬态
            raise GenerationTransientError(
                f"生成请求传输失败: {type(exc).__name__}"
            ) from exc

    def _iterate(self, response_stream: Any) -> Iterator[str]:
        """逐段产出增量文本并校验响应结构（末次用量随流捕获）"""
        usage: tuple[int, int] | None = None
        for response in response_stream:
            status_code = getattr(response, "status_code", None)
            if status_code != 200:
                self._raise_for_error_response(response)
            choices = getattr(getattr(response, "output", None), "choices", None)
            if not isinstance(choices, list) or not choices:
                raise GenerationProtocolViolationError("响应缺少生成选择项")
            message = getattr(choices[0], "message", None)
            delta = getattr(message, "content", None)
            if not isinstance(delta, str):
                raise GenerationProtocolViolationError("响应缺少增量文本")
            response_usage = getattr(response, "usage", None)
            input_tokens = getattr(response_usage, "input_tokens", None)
            output_tokens = getattr(response_usage, "output_tokens", None)
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                usage = (input_tokens, output_tokens)
            if delta:
                yield delta
        self._last_usage = usage

    def _raise_for_error_response(self, response: Any) -> None:
        """把非 200 响应映射为领域错误（消息不含密钥与正文）"""
        code = getattr(response, "code", None)
        message = str(getattr(response, "message", "") or "")
        if code:
            error_type = resolve_code_error(str(code), _GENERATION_ERROR_FAMILY)
            # 限流族在生成侧为独立合同错误码，优先于共享口径的瞬态归类
            if error_type is GenerationTransientError and str(code).startswith(
                ("Throttling", "SystemFlowControl")
            ):
                raise GenerationRateLimitedError(
                    f"供应方返回错误码 {code}: {message}"
                )
            raise error_type(f"供应方返回错误码 {code}: {message}")
        status_code = getattr(response, "status_code", None)
        if status_code in (401, 403):
            raise GenerationAuthError(f"供应方拒绝认证: {status_code}")
        if status_code == 429:
            raise GenerationRateLimitedError(f"供应方限流: {status_code}")
        if isinstance(status_code, int) and 500 <= status_code < 600:
            raise GenerationTransientError(f"供应方服务瞬态不可用: {status_code}")
        raise GenerationProtocolViolationError(
            f"供应方响应状态异常: {status_code}"
        )
