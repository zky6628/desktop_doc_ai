# -*- coding: utf-8 -*-
"""上传文件安全策略

大小/批次限制与格式白名单统一定义，暂存器与后续的接口层都从这里
取值，禁止各自内联导致口径漂移。扩展名一律小写比较；
MIME 校验采用宽松策略：客户端声明的类型与扩展名矛盾时才拒绝。
"""
from dataclasses import dataclass

from app.domain.errors import UnsupportedFormatError

# 单文件大小上限（字节）：100 MB，流式写入过程中超限即时中断
MAX_FILE_BYTES = 100 * 1024 * 1024

# 单批次文件数上限：批次内逐文件独立接受或拒绝
MAX_BATCH_FILES = 50

# 流式写入块大小（字节）：1 MB，避免整文件读入内存
STREAM_CHUNK_BYTES = 1024 * 1024

# 失败/取消暂存的最长保留时间（小时）：到期由清理流程删除
STALE_STAGING_MAX_AGE_HOURS = 24

# 磁盘预检的最小剩余空间（字节）：按单文件上限预留，防止写满磁盘
MIN_FREE_DISK_BYTES = MAX_FILE_BYTES


@dataclass(frozen=True)
class UploadFormat:
    """允许上传的格式定义

    magic_required 为 False 的文本格式没有可靠文件头特征，
    仅做扩展名与 MIME 一致性校验；为 True 时必须通过真实格式检测
    """

    extension: str
    mime_type: str
    magic_required: bool
    # 可接受的声明 MIME（客户端上报值），声明缺失或通用二进制类型不受限
    accepted_mime_types: frozenset[str]


# 允许上传的格式白名单（按扩展名索引）
ALLOWED_FORMATS: dict[str, UploadFormat] = {
    fmt.extension: fmt
    for fmt in (
        UploadFormat(
            "txt", "text/plain", magic_required=False,
            accepted_mime_types=frozenset({"text/plain"}),
        ),
        UploadFormat(
            "md", "text/markdown", magic_required=False,
            accepted_mime_types=frozenset({"text/markdown", "text/plain"}),
        ),
        UploadFormat(
            "markdown", "text/markdown", magic_required=False,
            accepted_mime_types=frozenset({"text/markdown", "text/plain"}),
        ),
        UploadFormat(
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            magic_required=True,
            accepted_mime_types=frozenset({
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/zip",
            }),
        ),
        UploadFormat(
            "pdf", "application/pdf", magic_required=True,
            accepted_mime_types=frozenset({"application/pdf"}),
        ),
    )
}

# 视为未声明（放行）的通用 MIME：客户端无法可靠区分时的缺省上报值
GENERIC_MIME_TYPES = frozenset({"", "application/octet-stream"})


def find_format(filename: str) -> UploadFormat:
    """按文件名提取扩展名并查白名单

    :param filename: 用户可见文件名（只用于提取扩展名，不参与落盘路径）
    :return: 对应格式定义
    :raises UnsupportedFormatError: 无扩展名、扩展名异常或不在白名单
    """
    name = filename.strip()
    if "." not in name:
        raise UnsupportedFormatError("文件缺少扩展名，无法识别类型")
    extension = name.rsplit(".", 1)[1].lower()
    if not extension.isalnum() or len(extension) > 10:
        raise UnsupportedFormatError(f"异常的文件扩展名: .{extension}")
    fmt = ALLOWED_FORMATS.get(extension)
    if fmt is None:
        raise UnsupportedFormatError(f"不支持的文件类型: .{extension}")
    return fmt
