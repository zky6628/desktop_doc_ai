# -*- coding: utf-8 -*-
"""上传文件流式暂存与过期清理

上传内容以固定块流式写入受控暂存目录，过程中累计 SHA-256 与字节数，
超过大小上限即时中断删除；扩展名白名单、声明 MIME 一致性与真实格式
（magic）校验全部通过后，把随机临时名原子重命名为最终暂存名。
内部落盘名一律使用 UUIDv7 生成，绝不拼接用户文件名；所有路径解析后
必须仍位于暂存根目录内。清理只删除受控暂存目录内的过期条目，
不接受任意外部路径。
"""
import hashlib
import os
import shutil
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass

import filetype

from app.domain import file_policy
from app.domain.errors import (
    EmptyFileError,
    FileTooLargeError,
    InsufficientDiskSpaceError,
    PathUnsafeError,
    UnsupportedFormatError,
)
from app.domain.ids import uuid7

# magic 头扫描范围（字节）：覆盖 PDF 文件头与文件尾的加密标记
_MAGIC_SCAN_BYTES = 1024

# PDF 加密字典关键字：出现即视为加密文件，明确不可接受
_PDF_ENCRYPT_MARKER = b"/Encrypt"


@dataclass(frozen=True)
class StagedFile:
    """暂存结果：创建文档版本与导入任务所需的全部事实

    display_name 仅作为用户可见名称保存，不参与任何落盘路径
    """

    staging_path: str
    display_name: str
    sha256: str
    size_bytes: int
    mime_type: str
    extension: str


class UploadStagingStore:
    """受控暂存目录的流式写入与清理

    :param staging_root: 暂存根目录（不存在时自动创建）
    """

    def __init__(self, staging_root: str) -> None:
        self._root = os.path.realpath(os.path.abspath(staging_root))
        os.makedirs(self._root, exist_ok=True)

    def stage(
        self,
        chunks: Iterable[bytes],
        display_name: str,
        declared_mime: str | None = None,
    ) -> StagedFile:
        """把上传流写入暂存并完成安全校验，返回暂存事实

        任一校验失败时删除临时文件并抛出对应领域错误，暂存目录无残留；
        成功时以随机 UUID 名原子落位，扩展名取自白名单（已规范化）。

        :param chunks: 上传内容块迭代器（调用方按块读取上传流）
        :param display_name: 用户可见文件名（只用于扩展名识别与展示）
        :param declared_mime: 客户端声明的 MIME（可空；与扩展名矛盾才拒绝）
        :raises UnsupportedFormatError: 扩展名/MIME/真实格式不一致或容器损坏
        :raises FileTooLargeError: 内容超过单文件大小上限
        :raises EmptyFileError: 内容为空
        :raises InsufficientDiskSpaceError: 磁盘剩余空间不足
        :raises PathUnsafeError: 落盘路径逃出受控根目录
        """
        fmt = file_policy.find_format(display_name)
        self._check_declared_mime(fmt, declared_mime)
        self._check_free_disk()

        temp_path = self._guard_within_root(
            os.path.join(self._root, uuid7() + ".part")
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with open(temp_path, "wb") as f:
                for chunk in chunks:
                    size += len(chunk)
                    if size > file_policy.MAX_FILE_BYTES:
                        raise FileTooLargeError(
                            "文件超过大小上限"
                            f" {file_policy.MAX_FILE_BYTES // (1024 * 1024)} MB"
                        )
                    digest.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())

            if size == 0:
                raise EmptyFileError("上传文件内容为空")
            self._check_real_format(temp_path, fmt)

            final_path = self._guard_within_root(
                os.path.join(self._root, uuid7() + "." + fmt.extension)
            )
            # 校验全部通过后同卷原子改名；改名后 finally 中不会误删
            os.replace(temp_path, final_path)
        finally:
            # 校验失败或写入异常时清空临时文件（成功改名后已不存在）
            if os.path.exists(temp_path):
                os.remove(temp_path)

        return StagedFile(
            staging_path=final_path,
            display_name=display_name,
            sha256=digest.hexdigest(),
            size_bytes=size,
            mime_type=fmt.mime_type,
            extension=fmt.extension,
        )

    def cleanup_stale_staging(self, max_age_hours: int | None = None) -> int:
        """删除超过保留期的暂存文件，返回删除数量

        失败/取消的暂存最迟限期清理；被占用或权限不足的条目跳过，
        留待下一轮清理。只扫描受控暂存根目录，不接受任意路径。

        :param max_age_hours: 保留期（小时），缺省使用策略默认值
        """
        hours = (
            file_policy.STALE_STAGING_MAX_AGE_HOURS
            if max_age_hours is None
            else max_age_hours
        )
        cutoff = time.time() - hours * 3600
        removed = 0
        for name in os.listdir(self._root):
            path = os.path.join(self._root, name)
            if not os.path.isfile(path):
                continue
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _check_declared_mime(
        fmt: file_policy.UploadFormat, declared_mime: str | None
    ) -> None:
        """声明 MIME 与扩展名矛盾时拒绝；缺失或通用二进制类型放行"""
        normalized = (declared_mime or "").strip().lower()
        if normalized in file_policy.GENERIC_MIME_TYPES:
            return
        if normalized not in fmt.accepted_mime_types:
            raise UnsupportedFormatError(
                f"声明的文件类型 {normalized} 与扩展名 .{fmt.extension} 不一致"
            )

    def _check_free_disk(self) -> None:
        """磁盘预检：暂存根所在磁盘剩余空间低于单文件上限时拒绝接收"""
        usage = shutil.disk_usage(self._root)
        if usage.free < file_policy.MIN_FREE_DISK_BYTES:
            raise InsufficientDiskSpaceError("磁盘剩余空间不足，无法接收文件")

    def _guard_within_root(self, path: str) -> str:
        """校验路径解析后仍位于暂存根目录内，防止符号链接逃逸"""
        if not os.path.realpath(path).startswith(self._root + os.sep):
            raise PathUnsafeError("落盘路径逃出受控暂存目录")
        return path

    @staticmethod
    def _check_real_format(temp_path: str, fmt: file_policy.UploadFormat) -> None:
        """真实格式（magic）校验：带 magic 要求的格式必须与声明一致"""
        if not fmt.magic_required:
            return

        kind = filetype.guess(temp_path)
        if kind is None or kind.extension != fmt.extension:
            raise UnsupportedFormatError(
                f"文件真实格式与扩展名 .{fmt.extension} 不一致或文件损坏"
            )

        if fmt.extension == "docx":
            # DOCX 是 ZIP 容器：验证可打开且包含 Word 内容目录，
            # 加密文档（OLE 复合容器）已被上面的格式不一致拒绝
            try:
                with zipfile.ZipFile(temp_path) as archive:
                    if not any(
                        name.startswith("word/") for name in archive.namelist()
                    ):
                        raise UnsupportedFormatError(
                            "文件不是有效的 DOCX 文档"
                        )
            except zipfile.BadZipFile as exc:
                raise UnsupportedFormatError("文件损坏或不是有效的 DOCX 文档") from exc

        if fmt.extension == "pdf" and _contains_encrypt_marker(temp_path):
            raise UnsupportedFormatError("暂不支持加密的 PDF 文件")


def _contains_encrypt_marker(path: str) -> bool:
    """扫描 PDF 文件头尾，判断是否携带加密字典标记"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(_MAGIC_SCAN_BYTES)
        if _PDF_ENCRYPT_MARKER in head:
            return True
        if size > _MAGIC_SCAN_BYTES:
            f.seek(max(0, size - _MAGIC_SCAN_BYTES))
            tail = f.read(_MAGIC_SCAN_BYTES)
            if _PDF_ENCRYPT_MARKER in tail:
                return True
    return False
