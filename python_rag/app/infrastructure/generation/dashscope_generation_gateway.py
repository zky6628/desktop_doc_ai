# -*- coding: utf-8 -*-
"""DashScope 生成适配器：OpenAI 兼容端点流式调用与错误分类

供应方新版生成模型经 OpenAI 兼容端点服务（原生端点不承载），网关以
openai SDK 流式调用并逐段产出增量文本；思考型模型的推理增量与回答
增量分离，网关关闭思考（引用问答场景思考会占用输出预算并拖慢首
token）。响应结构按协议校验：choices 缺失即协议违规，角色增量与空
增量跳过，末次用量（含 usage 专用收尾块）随流捕获。错误分类边界与
向量化/重排网关一致：连接层失败归瞬态，携带状态码的 HTTP 错误按业
务码映射（限流族单列为查询合同错误码），认证/配额归服务端配置问题，
未知状态按协议违规。客户端在构造时建立（连接池跨查询复用），请求
超时经 request_timeout 注入。密钥只经构造器注入，不进入日志与错误
消息。
"""
from collections.abc import Callable, Iterator, Sequence
from typing import Any, cast

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

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

# 供应方 OpenAI 兼容端点（原生端点不承载新版生成模型）
_COMPAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 本网关的领域错误族：限流单列（查询合同独立错误码），其余经共享口径
_GENERATION_ERROR_FAMILY = ErrorFamily(
    auth=GenerationAuthError,
    quota=GenerationQuotaError,
    transient=GenerationTransientError,
    protocol=GenerationProtocolViolationError,
)


class DashScopeGenerationGateway(GenerationGatewayPort):
    """DashScope 流式生成网关（OpenAI 兼容端点）

    :param api_key: API Key（构造器注入，绝不写日志与消息）
    :param invoker: 供应方调用函数（缺省绑定 SDK 客户端，测试注入替身）
    :param model: 生成模型名
    :param max_tokens: 输出 token 上限
    :param request_timeout: 单次流式请求超时（秒，约束流式读取）
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
        self._model = model
        self._max_tokens = max_tokens
        self._request_timeout = request_timeout
        self._last_usage: tuple[int, int] | None = None
        if invoker is not None:
            self._invoke = invoker
        else:
            # 客户端构造一次：连接池跨查询复用，避免每题支付建连成本
            self._client = OpenAI(
                api_key=api_key,
                base_url=_COMPAT_BASE_URL,
                max_retries=0,
            )
            self._invoke = self._invoke_client

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
        except GenerationError:
            raise
        except Exception as exc:
            # 调用边界兜底：携带状态码的 HTTP 错误按业务码映射，
            # 连接层失败统一归瞬态，消息只含异常类型名
            if getattr(exc, "status_code", None) is not None:
                self._raise_for_api_error(exc)
            raise GenerationTransientError(
                f"生成请求传输失败: {type(exc).__name__}"
            ) from exc
        return self._iterate(response_stream)

    def _invoke_client(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        request_timeout: int,
        api_key: str,
    ) -> Any:
        """SDK 客户端的默认调用绑定（测试注入替身替换）

        关闭思考：引用问答场景思考增量会占用输出预算并拖慢首 token，
        作为冻结口径随配置行登记。
        """
        return self._client.chat.completions.create(
            model=model,
            messages=cast("list[ChatCompletionMessageParam]", messages),
            max_tokens=max_tokens,
            timeout=request_timeout,
            stream=True,
            stream_options={"include_usage": True},
            extra_body={"enable_thinking": False},
        )

    def _iterate(self, response_stream: Any) -> Iterator[str]:
        """逐段产出增量文本并校验响应结构（末次用量随流捕获）"""
        usage: tuple[int, int] | None = None
        for chunk in response_stream:
            chunk_usage = getattr(chunk, "usage", None)
            input_tokens = getattr(chunk_usage, "prompt_tokens", None)
            output_tokens = getattr(chunk_usage, "completion_tokens", None)
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                usage = (input_tokens, output_tokens)
            choices = getattr(chunk, "choices", None)
            # 用量专用收尾块无 choices，跳过；形状缺失属协议违规
            if not isinstance(choices, list):
                raise GenerationProtocolViolationError("响应缺少生成选择项")
            if not choices:
                continue
            delta = getattr(getattr(choices[0], "delta", None), "content", None)
            if delta:
                yield delta
        self._last_usage = usage

    def _raise_for_api_error(self, exc: Any) -> None:
        """把携带状态码的 HTTP 错误映射为领域错误（消息不含密钥与正文）"""
        body = getattr(exc, "body", None)
        code = None
        message = ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = str(error.get("message") or "")
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
        status_code = getattr(exc, "status_code", None)
        if status_code in (401, 403):
            raise GenerationAuthError(f"供应方拒绝认证: {status_code}")
        if status_code == 429:
            raise GenerationRateLimitedError(f"供应方限流: {status_code}")
        if isinstance(status_code, int) and 500 <= status_code < 600:
            raise GenerationTransientError(f"供应方服务瞬态不可用: {status_code}")
        raise GenerationProtocolViolationError(
            f"供应方响应状态异常: {status_code}"
        )
