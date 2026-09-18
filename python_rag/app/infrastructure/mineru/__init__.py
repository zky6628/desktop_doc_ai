# -*- coding: utf-8 -*-
"""MinerU 云端解析适配器：批量提交、预签名上传、轮询与安全解压

DTO 与原始响应不越出本包；上层只接触客户端方法返回的值对象。
"""
from .client import MinerUClient
from .dto import BatchFileEntry, BatchPollResult, BatchSubmission, ProviderFileStatus
from .polling import poll_batch_until_done

__all__ = [
    "BatchFileEntry",
    "BatchPollResult",
    "BatchSubmission",
    "MinerUClient",
    "ProviderFileStatus",
    "poll_batch_until_done",
]
