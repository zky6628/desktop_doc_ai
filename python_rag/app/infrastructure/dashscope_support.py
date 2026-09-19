# -*- coding: utf-8 -*-
"""DashScope 供应方错误分类的共享口径

业务错误码到领域错误类别的映射与瞬态 HTTP 状态分层在文本向量化与
重排两个供应方网关间共享；各网关以 ErrorFamily 声明自己的错误族，
映射逻辑单点维护。
"""
from dataclasses import dataclass

# 业务错误码到错误类别的映射（供应方文档口径）；
# 前缀类码（限流族）在解析函数中按后缀细分，未列出的码按协议违规
_CODE_ERROR_MAP: dict[str, str] = {
    "InvalidApiKey": "auth",
    "InvalidApiKey.NotFound": "auth",
    "AccessDenied": "auth",
    "Arrearage": "quota",
}

# 瞬态类错误码：限流族与系统流控（配额族后缀在解析时优先细分）
_TRANSIENT_CODE_PREFIXES = ("Throttling", "SystemFlowControl")

# 配额族后缀：限流码携带该后缀时语义为额度耗尽而非请求速率
_QUOTA_CODE_MARKER = "AllocationQuota"


@dataclass(frozen=True)
class ErrorFamily:
    """供应方网关的领域错误族（按类别提供错误类）"""

    auth: type[Exception]
    quota: type[Exception]
    transient: type[Exception]
    protocol: type[Exception]


def resolve_code_error(code: str, family: ErrorFamily) -> type[Exception]:
    """按供应方业务错误码解析错误类别

    限流族优先细分：携带配额后缀的语义为额度耗尽（不可自动重试），
    其余限流为请求速率类瞬态；未列出的码按协议违规处理
    """
    if _QUOTA_CODE_MARKER in code or code == "Arrearage":
        return family.quota
    if any(code.startswith(prefix) for prefix in _TRANSIENT_CODE_PREFIXES):
        return family.transient
    mapped = _CODE_ERROR_MAP.get(code)
    if mapped is not None:
        return getattr(family, mapped)
    return family.protocol


def is_transient_status(status_code: int) -> bool:
    """按 HTTP 状态分层判断传输层瞬态（无业务码时的回退口径）"""
    return status_code in (408, 429) or 500 <= status_code < 600
