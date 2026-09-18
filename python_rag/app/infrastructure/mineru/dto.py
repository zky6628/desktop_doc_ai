# -*- coding: utf-8 -*-
"""MinerU 响应 DTO：供应商响应的边界表示

DTO 只存在于基础设施适配器内部；向领域/应用层暴露的只有适配器
方法返回的这些值对象，供应商原始响应 JSON 不越出本包。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BatchFileEntry:
    """批量提交中的单个文件条目

    source_ref 作为供应方 data_id 回传，轮询结果按它关联；
    display_name 仅用于供应方侧展示与格式识别
    """

    source_ref: str
    display_name: str


@dataclass(frozen=True)
class BatchSubmission:
    """批量提交结果：批次引用与按请求位置对应的预签名上传地址

    上传地址只驻留内存；过期时间按申请时刻加供应商链接有效期推算，
    供持久化层记录
    """

    batch_id: str
    upload_urls: tuple[str, ...]
    upload_url_expires_at: str


@dataclass(frozen=True)
class ProviderFileStatus:
    """批次内单文件的供应方侧状态

    source_ref 来自供应方回传的 data_id；结果地址只在轮询快照的
    内存表示中出现，摘要与日志不得携带
    """

    source_ref: str
    state: str
    full_zip_url: str | None = None
    err_msg: str | None = None
    extracted_pages: int | None = None
    total_pages: int | None = None

    @property
    def status_summary(self) -> str:
        """脱敏状态摘要（不含任何 URL）：持久化到供应方状态摘要列"""
        if self.state == "running" and self.total_pages:
            return f"running {self.extracted_pages}/{self.total_pages} pages"
        return self.state

    @property
    def is_terminal(self) -> bool:
        """供应方侧终态：成功或失败，无需继续轮询"""
        return self.state in ("done", "failed")


@dataclass(frozen=True)
class BatchPollResult:
    """一次批次轮询的状态快照"""

    batch_id: str
    files: tuple[ProviderFileStatus, ...]
