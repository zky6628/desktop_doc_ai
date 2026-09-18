# -*- coding: utf-8 -*-
"""MinerU 适配器：批量提交、预签名上传、批量轮询与结果下载

主流程固定为申请上传链接 -> 逐文件 PUT -> 批量轮询 -> 下载结果压缩包；
只维护这一条主实现。预签名地址（上传与下载）只存在于内存表示中，
错误消息与摘要一律脱敏。供应方业务错误码按官方文档归类为
认证/配额/输入拒绝/协议违规/瞬态五类，未知码一律按协议违规处理。
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Self
from urllib.parse import urlsplit

import httpx

from app.domain.errors import (
    ArchiveRejectedError,
    CloudAuthError,
    CloudInputRejectedError,
    CloudProtocolViolationError,
    CloudQuotaError,
    CloudTransportError,
)
from app.infrastructure.mineru.archive import extract_archive

from .dto import BatchFileEntry, BatchPollResult, BatchSubmission, ProviderFileStatus

# 供应方 API 基地址与接口路径（主流程固定，不维护第二套实现）
DEFAULT_BASE_URL = "https://mineru.net"
_BATCH_URL_PATH = "/api/v4/file-urls/batch"
_POLL_URL_PATH = "/api/v4/extract-results/batch/{batch_id}"

# 预签名上传链接有效期（小时）：申请时刻起 24 小时内完成上传
UPLOAD_URL_TTL_HOURS = 24

# 上传与下载的固定块大小（字节）：不整文件读入内存
_TRANSFER_CHUNK_BYTES = 1024 * 1024

# 结果下载限制：重定向次数与响应体大小上限
_DOWNLOAD_MAX_REDIRECTS = 3
_DOWNLOAD_MAX_BYTES = 500 * 1024 * 1024

# 结果文件允许的主机后缀（带点前缀表示子域匹配）：结果压缩包托管在
# 供应方内容分发域，可用环境变量覆盖以应对域名调整
_DEFAULT_DOWNLOAD_HOST_SUFFIXES = (".mineru.net", ".openxlab.org.cn")
_DOWNLOAD_HOST_SUFFIXES_ENV = "MINERU_RESULT_HOST_ALLOWLIST"

# 轮询支持的任务状态全集；未知状态视为协议违规
_PROVIDER_STATES = frozenset({
    "done", "waiting-file", "pending", "running", "failed", "converting",
})

# 供应方业务错误码到错误类别的映射（官方错误码文档口径）；
# 未列出的码一律按协议违规处理
_CODE_ERROR_MAP: dict[int, type[Exception]] = {
    -60001: CloudTransportError,   # 生成上传 URL 失败，请稍后再试
    -60007: CloudTransportError,   # 模型服务暂时不可用
    -60009: CloudTransportError,   # 任务提交队列已满，请稍后再试
    -60010: CloudTransportError,   # 解析失败，请稍后再试
    -10001: CloudTransportError,   # 服务异常，请稍后再试
    -60018: CloudQuotaError,       # 每日解析任务数量已达上限
    -60019: CloudQuotaError,       # html 解析额度不足
    -60002: CloudInputRejectedError,  # 获取匹配的文件格式失败
    -60003: CloudInputRejectedError,  # 文件读取失败
    -60004: CloudInputRejectedError,  # 空文件
    -60005: CloudInputRejectedError,  # 文件大小超出限制
    -60006: CloudInputRejectedError,  # 文件页数超过限制
    -60008: CloudInputRejectedError,  # 文件读取超时
    -60011: CloudInputRejectedError,  # 获取有效文件失败
    -60017: CloudInputRejectedError,  # 重试次数达到上限
}

# HTTP 状态码到错误类别的映射
_STATUS_ERROR_MAP: dict[int, type[Exception]] = {
    401: CloudAuthError,
    403: CloudAuthError,
    408: CloudTransportError,
    429: CloudTransportError,
}


def default_download_host_suffixes() -> tuple[str, ...]:
    """结果下载允许的主机后缀：环境变量可覆盖默认值

    :return: 逗号分隔环境变量的后缀元组；未设置时使用默认值
    """
    configured = os.getenv(_DOWNLOAD_HOST_SUFFIXES_ENV)
    if not configured:
        return _DEFAULT_DOWNLOAD_HOST_SUFFIXES
    return tuple(
        suffix.strip().lower()
        for suffix in configured.split(",")
        if suffix.strip()
    )


def _raise_for_response_status(status_code: int) -> None:
    """按 HTTP 状态码映射传输层失败

    :param status_code: 供应方响应状态码
    :raises CloudAuthError: 401/403
    :raises CloudTransportError: 408/429/5xx
    :raises CloudProtocolViolationError: 其他非成功状态
    """
    if status_code in _STATUS_ERROR_MAP:
        raise _STATUS_ERROR_MAP[status_code]("供应商响应异常状态码")
    if status_code >= 500:
        raise CloudTransportError("供应商服务暂不可用")
    if status_code >= 400:
        raise CloudProtocolViolationError(
            f"供应商响应状态码异常: {status_code}"
        )


def _raise_for_body_code(code: int, msg: str) -> None:
    """按供应方业务错误码映射失败类别

    :param code: 响应体业务码（成功为 0）
    :param msg: 响应体说明文本（供应商原文，不含敏感信息）
    """
    if code == 0:
        return
    error_type = _CODE_ERROR_MAP.get(code, CloudProtocolViolationError)
    raise error_type(f"供应商返回错误码 {code}: {msg}")


class MinerUClient:
    """MinerU HTTP 适配器

    :param api_token: API Token（Authorization: Bearer）
    :param base_url: API 基地址
    :param transport: 可注入的 httpx 传输层（测试用 Mock 替换）
    :param timeout_seconds: 单请求超时（秒）
    :param model_version: 解析模型版本（pipeline/vlm），提交批次时统一携带
    """

    PROVIDER_NAME = "mineru"

    def __init__(
        self,
        *,
        api_token: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 30.0,
        model_version: str = "pipeline",
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout_seconds,
        )
        self._token = api_token
        self._model_version = model_version

    def close(self) -> None:
        """关闭底层 HTTP 连接池"""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def create_batch(self, entries: list[BatchFileEntry]) -> BatchSubmission:
        """申请批量上传链接

        响应缺少批次 ID、上传地址数量与请求不一致、业务码非零或
        schema 不符时整体失败，不执行任何文件上传。

        :param entries: 批次文件条目（顺序即上传地址顺序）
        :return: 批次提交结果（地址按请求位置对应）
        :raises CloudAuthError: Token 无效或过期
        :raises CloudQuotaError: 供应商配额不足
        :raises CloudInputRejectedError: 供应商拒绝输入
        :raises CloudTransportError: 网络或供应商服务瞬态故障
        :raises CloudProtocolViolationError: 响应 schema 不符
        """
        payload = {
            "model_version": self._model_version,
            "files": [
                {"name": entry.display_name, "data_id": entry.source_ref}
                for entry in entries
            ],
        }
        try:
            response = self._client.post(
                _BATCH_URL_PATH,
                json=payload,
                headers=self._api_headers(),
            )
        except httpx.HTTPError as exc:
            raise CloudTransportError("申请上传链接网络失败") from exc
        _raise_for_response_status(response.status_code)
        body = self._json_body(response)
        _raise_for_body_code(body.get("code", -1), str(body.get("msg", "")))
        data = body.get("data")
        if not isinstance(data, dict) or not data.get("batch_id"):
            raise CloudProtocolViolationError("响应缺少批次 ID")
        upload_urls = data.get("file_urls")
        if not isinstance(upload_urls, list) or len(upload_urls) != len(entries):
            raise CloudProtocolViolationError("上传地址数量与请求不一致")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=UPLOAD_URL_TTL_HOURS)
        ).isoformat(timespec="seconds")
        return BatchSubmission(
            batch_id=str(data["batch_id"]),
            upload_urls=tuple(str(url) for url in upload_urls),
            upload_url_expires_at=expires_at,
        )

    def upload_file(self, upload_url: str, file_path: str) -> None:
        """把本地暂存文件 PUT 到预签名地址

        按固定块流式读取并显式声明 Content-Length（对象存储预签名
        上传要求）；请求不携带任何认证头。

        :param upload_url: 预签名上传地址（只驻留内存）
        :param file_path: 本地暂存文件路径
        :raises CloudTransportError: 网络故障或供应方存储瞬态错误
        :raises CloudProtocolViolationError: 上传响应状态异常
        """
        size = os.path.getsize(file_path)
        headers = {"Content-Length": str(size)}

        def _blocks():
            with open(file_path, "rb") as f:
                while block := f.read(_TRANSFER_CHUNK_BYTES):
                    yield block

        try:
            response = self._client.put(
                upload_url, content=_blocks(), headers=headers
            )
        except httpx.HTTPError as exc:
            raise CloudTransportError("上传文件网络失败") from exc
        _raise_for_response_status(response.status_code)

    def poll_batch(self, batch_id: str) -> BatchPollResult:
        """查询批次内全部文件的当前状态

        结果按供应方回传的 data_id 关联（不依赖数组顺序与文件名）；
        缺少 data_id 的条目与未知状态均按协议违规处理。

        :param batch_id: 批次引用
        :return: 按源引用索引的状态快照
        :raises CloudAuthError: Token 无效或过期
        :raises CloudTransportError: 网络或供应商服务瞬态故障
        :raises CloudProtocolViolationError: 响应 schema 不符
        """
        try:
            response = self._client.get(
                _POLL_URL_PATH.format(batch_id=batch_id),
                headers=self._api_headers(),
            )
        except httpx.HTTPError as exc:
            raise CloudTransportError("轮询网络失败") from exc
        _raise_for_response_status(response.status_code)
        body = self._json_body(response)
        _raise_for_body_code(body.get("code", -1), str(body.get("msg", "")))
        data = body.get("data")
        if not isinstance(data, dict) or not data.get("batch_id"):
            raise CloudProtocolViolationError("轮询响应缺少批次 ID")
        raw_results = data.get("extract_result")
        if not isinstance(raw_results, list):
            raise CloudProtocolViolationError("轮询响应缺少结果数组")

        statuses: list[ProviderFileStatus] = []
        for item in raw_results:
            if not isinstance(item, dict) or not item.get("data_id"):
                raise CloudProtocolViolationError("轮询结果缺少源引用")
            state = item.get("state")
            if state not in _PROVIDER_STATES:
                raise CloudProtocolViolationError(f"未知的任务状态: {state}")
            progress = item.get("extract_progress") or {}
            statuses.append(
                ProviderFileStatus(
                    source_ref=str(item["data_id"]),
                    state=str(state),
                    full_zip_url=item.get("full_zip_url"),
                    err_msg=item.get("err_msg") or None,
                    extracted_pages=progress.get("extracted_pages"),
                    total_pages=progress.get("total_pages"),
                )
            )
        return BatchPollResult(
            batch_id=str(data["batch_id"]), files=tuple(statuses)
        )

    def download_result(
        self,
        result_url: str,
        dest_path: str,
        *,
        max_bytes: int = _DOWNLOAD_MAX_BYTES,
        host_suffixes: tuple[str, ...] | None = None,
    ) -> str:
        """把结果压缩包下载到指定路径并计算内容哈希

        逐跳校验 scheme 与主机后缀允许列表，限制重定向次数与响应体
        大小，按固定块流式写入；内容哈希供下载后的完整性验证。

        :param result_url: 结果压缩包地址（只驻留内存）
        :param dest_path: 下载目标路径（由调用方在受控暂存目录内指定）
        :param max_bytes: 响应体大小上限（字节）
        :param host_suffixes: 主机后缀允许列表（缺省取默认配置）
        :return: 下载内容的 SHA-256 十六进制文本
        :raises ArchiveRejectedError: 地址不符合安全合同或超过大小上限
        :raises CloudTransportError: 网络故障或供应方存储瞬态错误
        """
        suffixes = (
            host_suffixes if host_suffixes is not None
            else default_download_host_suffixes()
        )
        current_url = result_url
        for _ in range(_DOWNLOAD_MAX_REDIRECTS + 1):
            self._check_download_target(current_url, suffixes)
            try:
                response = self._client.get(current_url)
            except httpx.HTTPError as exc:
                raise CloudTransportError("下载结果网络失败") from exc
            if response.is_redirect:
                current_url = response.headers.get("Location", "")
                continue
            _raise_for_response_status(response.status_code)
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ArchiveRejectedError("下载内容超过大小上限")
            digest = hashlib.sha256()
            received = 0
            with open(dest_path, "wb") as f:
                for block in response.iter_bytes(_TRANSFER_CHUNK_BYTES):
                    received += len(block)
                    if received > max_bytes:
                        raise ArchiveRejectedError("下载内容超过大小上限")
                    digest.update(block)
                    f.write(block)
            return digest.hexdigest()
        raise ArchiveRejectedError("下载重定向次数超过限制")

    def extract_result_archive(self, zip_path: str, dest_dir: str) -> list[str]:
        """安全解压结果压缩包（校验规则见归档模块）

        :param zip_path: 已下载的压缩包路径
        :param dest_dir: 解压目标目录（受控暂存目录内的子目录）
        :return: 解压出的相对路径列表
        :raises ArchiveRejectedError: 压缩包未通过安全校验
        """
        return extract_archive(zip_path, dest_dir)

    def _api_headers(self) -> dict[str, str]:
        """API 请求头：Bearer Token 只用于供应方 API 域"""
        return {"Authorization": f"Bearer {self._token}"}

    @staticmethod
    def _check_download_target(url: str, suffixes: tuple[str, ...]) -> None:
        """校验下载目标地址的安全合同

        :param url: 当前跳转目标地址
        :param suffixes: 主机后缀允许列表
        :raises ArchiveRejectedError: scheme 或主机不在允许范围
        """
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ArchiveRejectedError("下载地址协议不符合安全要求")
        host = (parts.hostname or "").lower()
        if not any(host == s.lstrip(".") or host.endswith(s) for s in suffixes):
            raise ArchiveRejectedError("下载地址主机不在允许列表")

    @staticmethod
    def _json_body(response: httpx.Response) -> dict:
        """解析 JSON 响应体，非对象结构按协议违规处理

        :param response: 供应方响应
        :return: 响应体字典
        :raises CloudProtocolViolationError: 响应体不是 JSON 对象
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise CloudProtocolViolationError("响应不是有效的 JSON") from exc
        if not isinstance(body, dict):
            raise CloudProtocolViolationError("响应体结构不符合协议")
        return body
