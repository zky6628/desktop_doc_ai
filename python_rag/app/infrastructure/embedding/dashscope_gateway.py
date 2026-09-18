# -*- coding: utf-8 -*-
"""DashScope 文本向量化适配器：批量调用、对齐校验与错误分类

供应方单请求文本数有上限，网关按固定批大小逐批调用并保持输入顺序；
每批响应按 text_index 排序后与输入做数量、索引连续性与维度对齐校验。
错误分类边界：调用抛出异常即传输层瞬态（供应方业务错误一律经响应体
表达）；返回响应按业务错误码映射，无业务码的非 200 按 HTTP 状态分层
处理，未知业务码按协议违规处理——与云端解析适配器的口径一致。密钥
只经构造器注入，不进入任何日志与错误消息。
"""
from collections.abc import Callable, Sequence
from typing import Any

from dashscope import TextEmbedding

from app.domain import embedding
from app.domain.errors import (
    EmbeddingAuthError,
    EmbeddingProtocolViolationError,
    EmbeddingQuotaError,
    EmbeddingTransientError,
)
from app.domain.ports import EmbeddingGateway as EmbeddingGatewayPort

# 业务错误码到错误类别的映射（供应方文档口径）；
# 前缀类码（限流族）在解析函数中按后缀细分，未列出的码按协议违规
_CODE_ERROR_MAP: dict[str, type[Exception]] = {
    "InvalidApiKey": EmbeddingAuthError,
    "InvalidApiKey.NotFound": EmbeddingAuthError,
    "AccessDenied": EmbeddingAuthError,
    "Arrearage": EmbeddingQuotaError,
    "AllocationQuota": EmbeddingQuotaError,
}

# 瞬态类错误码：限流族与系统流控（配额族后缀在解析时优先细分）
_TRANSIENT_CODE_PREFIXES = ("Throttling", "SystemFlowControl")

# 配额族后缀：限流码携带该后缀时语义为额度耗尽而非请求速率
_QUOTA_CODE_MARKER = "AllocationQuota"


def _invoke_sdk(
    model: str,
    input_texts: list[str],
    dimensions: int,
    text_type: str,
    api_key: str,
) -> Any:
    """DashScope SDK 的默认调用绑定（测试注入替身替换）"""
    return TextEmbedding.call(
        model=model,
        input=input_texts,
        dimension=dimensions,
        text_type=text_type,
        api_key=api_key,
    )


def _resolve_code_error(code: str) -> type[Exception]:
    """按供应方业务错误码解析错误类别

    限流族优先细分：携带配额后缀的语义为额度耗尽（不可自动重试），
    其余限流为请求速率类瞬态；未列出的码按协议违规处理
    """
    if _QUOTA_CODE_MARKER in code or code == "Arrearage":
        return EmbeddingQuotaError
    if any(code.startswith(prefix) for prefix in _TRANSIENT_CODE_PREFIXES):
        return EmbeddingTransientError
    mapped = _CODE_ERROR_MAP.get(code)
    if mapped is not None:
        return mapped
    return EmbeddingProtocolViolationError


def _is_transient_status(status_code: int) -> bool:
    """按 HTTP 状态分层判断传输层瞬态（无业务码时的回退口径）"""
    return status_code in (408, 429) or 500 <= status_code < 600


class DashScopeEmbeddingGateway(EmbeddingGatewayPort):
    """DashScope 文本向量化网关

    :param api_key: API Key（构造器注入，绝不写日志与消息）
    :param invoker: 供应方调用函数（缺省绑定 SDK，测试注入替身）
    :param model: 向量模型名
    :param dimensions: 向量维度（响应校验基准）
    :param batch_size: 单请求文本数上限
    :param text_type: 文本侧别（文档构建固定 document）
    """

    def __init__(
        self,
        *,
        api_key: str,
        invoker: Callable[[str, list[str], int, str, str], Any] | None = None,
        model: str = embedding.EMBEDDING_MODEL,
        dimensions: int = embedding.EMBEDDING_DIMENSIONS,
        batch_size: int = embedding.EMBEDDING_BATCH_SIZE,
        text_type: str = embedding.EMBEDDING_TEXT_TYPE,
    ) -> None:
        self._api_key = api_key
        self._invoke = invoker if invoker is not None else _invoke_sdk
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._text_type = text_type

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """把文本序列向量化（方法契约见领域 Port 定义）"""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """单批向量化：传输层异常统一归瞬态，业务层按响应内容分类"""
        try:
            response = self._invoke(
                self._model, batch, self._dimensions, self._text_type, self._api_key
            )
        except Exception as exc:
            # 调用边界兜底：SDK 传输层失败以异常表达，统一归瞬态
            raise EmbeddingTransientError(
                f"向量化请求传输失败: {type(exc).__name__}"
            ) from exc
        return self._extract_batch_vectors(response, len(batch))

    def _extract_batch_vectors(
        self, response: Any, batch_size: int
    ) -> list[list[float]]:
        """从响应提取本批向量并完成对齐校验"""
        if getattr(response, "status_code", None) != 200:
            self._raise_for_error_response(response)

        output = getattr(response, "output", None)
        raw_items = output.get("embeddings") if isinstance(output, dict) else None
        if not isinstance(raw_items, list) or len(raw_items) != batch_size:
            raise EmbeddingProtocolViolationError("响应向量数量与输入不一致")

        indexed = [self._text_index(item) for item in raw_items]
        ordered = sorted(zip(indexed, raw_items), key=lambda pair: pair[0])
        vectors: list[list[float]] = []
        for position, (index, item) in enumerate(ordered):
            if index != position:
                raise EmbeddingProtocolViolationError("响应文本序号不连续")
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list) or not vector:
                raise EmbeddingProtocolViolationError("响应缺少向量内容")
            if len(vector) != self._dimensions:
                raise EmbeddingProtocolViolationError("响应向量维度与配置不一致")
            vectors.append([float(value) for value in vector])
        return vectors

    @staticmethod
    def _text_index(item: Any) -> int:
        """读取响应条目的文本序号；缺失或非整数按协议违规处理"""
        index = item.get("text_index") if isinstance(item, dict) else None
        if not isinstance(index, int):
            raise EmbeddingProtocolViolationError("响应缺少文本序号")
        return index

    def _raise_for_error_response(self, response: Any) -> None:
        """把非 200 响应映射为领域错误（消息不含密钥与文本内容）"""
        status_code = getattr(response, "status_code", None)
        code = getattr(response, "code", None)
        message = str(getattr(response, "message", "") or "")
        if code:
            error_type = _resolve_code_error(str(code))
            raise error_type(f"供应方返回错误码 {code}: {message}")
        if status_code in (401, 403):
            raise EmbeddingAuthError(f"供应方拒绝认证: {status_code}")
        if isinstance(status_code, int) and _is_transient_status(status_code):
            raise EmbeddingTransientError(f"供应方服务瞬态不可用: {status_code}")
        raise EmbeddingProtocolViolationError(f"供应方响应状态异常: {status_code}")
