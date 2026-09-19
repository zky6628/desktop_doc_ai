# -*- coding: utf-8 -*-
"""DashScope 重排适配器：调用、结果校验与错误分类

供应方返回的下标指输入文档序列位置，适配器校验结果数量、下标在界
且不重复、分数为数值，再按相关度稳定重排返回；校验失败按协议违规
拒绝。错误分类边界与向量化网关一致：调用抛异常即传输层瞬态，返回
响应按业务错误码映射，无业务码非 200 按 HTTP 状态分层，未知业务码
按协议违规。请求超时经 request_timeout 注入（供应方 SDK 默认 300
秒，远超重排延迟预算）。密钥只经构造器注入，不进入日志与错误消息。
"""
from collections.abc import Callable, Sequence
from typing import Any

from dashscope import TextReRank

from app.domain import rerank
from app.domain.errors import (
    RerankAuthError,
    RerankProtocolViolationError,
    RerankQuotaError,
    RerankTransientError,
)
from app.domain.ports import RerankGateway as RerankGatewayPort
from app.domain.rerank import RerankHit

from ..dashscope_support import ErrorFamily, is_transient_status, resolve_code_error

# 本网关的领域错误族：业务错误码经共享口径映射到对应类别
_RERANK_ERROR_FAMILY = ErrorFamily(
    auth=RerankAuthError,
    quota=RerankQuotaError,
    transient=RerankTransientError,
    protocol=RerankProtocolViolationError,
)


def _invoke_sdk(
    model: str,
    query: str,
    documents: list[str],
    top_n: int,
    request_timeout: int,
    api_key: str,
) -> Any:
    """DashScope SDK 的默认调用绑定（测试注入替身替换）"""
    return TextReRank.call(
        model=model,
        query=query,
        documents=documents,
        top_n=top_n,
        request_timeout=request_timeout,
        api_key=api_key,
    )


class DashScopeRerankGateway(RerankGatewayPort):
    """DashScope 重排网关

    :param api_key: API Key（构造器注入，绝不写日志与消息）
    :param invoker: 供应方调用函数（缺省绑定 SDK，测试注入替身）
    :param model: 重排模型名
    :param top_n: 返回命中数上限
    :param request_timeout: 单请求超时（秒）
    """

    def __init__(
        self,
        *,
        api_key: str,
        invoker: Callable[[str, str, list[str], int, int, str], Any] | None = None,
        model: str = rerank.RERANK_MODEL,
        top_n: int = rerank.RERANK_TOP_N,
        request_timeout: int = rerank.RERANK_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._invoke = invoker if invoker is not None else _invoke_sdk
        self._model = model
        self._top_n = top_n
        self._request_timeout = request_timeout

    def rerank(self, question: str, documents: Sequence[str]) -> list[RerankHit]:
        """按问题相关性重排文档文本（方法契约见领域 Port 定义）"""
        if not documents:
            return []
        try:
            response = self._invoke(
                self._model,
                question,
                list(documents),
                self._top_n,
                self._request_timeout,
                self._api_key,
            )
        except Exception as exc:
            # 调用边界兜底：SDK 传输层失败以异常表达，统一归瞬态
            raise RerankTransientError(
                f"重排请求传输失败: {type(exc).__name__}"
            ) from exc
        return self._extract_results(response, len(documents))

    def _extract_results(
        self, response: Any, document_count: int
    ) -> list[RerankHit]:
        """从响应提取重排结果并完成数量/下标/分数校验"""
        if getattr(response, "status_code", None) != 200:
            self._raise_for_error_response(response)

        output = getattr(response, "output", None)
        raw_results = getattr(output, "results", None) if output is not None else None
        if not isinstance(raw_results, list):
            raise RerankProtocolViolationError("响应缺少重排结果")
        expected = min(self._top_n, document_count)
        if len(raw_results) != expected:
            raise RerankProtocolViolationError(
                f"响应结果数量与预期不符: {len(raw_results)} != {expected}"
            )

        hits: list[RerankHit] = []
        seen: set[int] = set()
        for item in raw_results:
            hits.append(self._to_hit(item, document_count, seen))
        # 稳定重排：相关度降序，同分按输入下标升序
        hits.sort(key=lambda hit: (-hit.score, hit.index))
        return hits

    @staticmethod
    def _to_hit(item: Any, document_count: int, seen: set[int]) -> RerankHit:
        """校验单条结果并转换为领域命中"""
        index = getattr(item, "index", None)
        if not isinstance(index, int) or isinstance(index, bool):
            raise RerankProtocolViolationError("响应缺少文档下标")
        if index < 0 or index >= document_count:
            raise RerankProtocolViolationError("响应文档下标越界")
        if index in seen:
            raise RerankProtocolViolationError("响应文档下标重复")
        seen.add(index)
        score = getattr(item, "relevance_score", None)
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise RerankProtocolViolationError("响应相关性分数类型非法")
        return RerankHit(index=index, score=float(score))

    def _raise_for_error_response(self, response: Any) -> None:
        """把非 200 响应映射为领域错误（消息不含密钥与文本内容）"""
        status_code = getattr(response, "status_code", None)
        code = getattr(response, "code", None)
        message = str(getattr(response, "message", "") or "")
        if code:
            error_type = resolve_code_error(str(code), _RERANK_ERROR_FAMILY)
            raise error_type(f"供应方返回错误码 {code}: {message}")
        if status_code in (401, 403):
            raise RerankAuthError(f"供应方拒绝认证: {status_code}")
        if isinstance(status_code, int) and is_transient_status(status_code):
            raise RerankTransientError(f"供应方服务瞬态不可用: {status_code}")
        raise RerankProtocolViolationError(f"供应方响应状态异常: {status_code}")
