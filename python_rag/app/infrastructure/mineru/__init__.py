# -*- coding: utf-8 -*-
"""MinerU 云端解析适配器：批量提交、预签名上传、轮询与安全解压

DTO 与原始响应不越出本包；上层只接触客户端方法返回的值对象。
归一化把解压产物目录转换为统一解析模型。
"""
from .client import MinerUClient
from .dto import BatchFileEntry, BatchPollResult, BatchSubmission, ProviderFileStatus
from .normalize import NORMALIZATION_CONFIG_VERSION, normalize_archive
from .polling import poll_batch_until_done

__all__ = [
    "NORMALIZATION_CONFIG_VERSION",
    "BatchFileEntry",
    "BatchPollResult",
    "BatchSubmission",
    "MinerUClient",
    "ProviderFileStatus",
    "normalize_archive",
    "poll_batch_until_done",
]
