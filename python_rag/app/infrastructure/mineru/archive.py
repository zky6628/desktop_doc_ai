# -*- coding: utf-8 -*-
"""结果压缩包安全解压

解压前对每个条目做静态校验：路径穿越、绝对路径、符号链接/设备
条目、单条目与总量大小、条目数量、压缩比（炸弹防御）与扩展名
白名单全部通过后才落盘；阈值锚定供应方产物结构（Markdown/JSON/
图片/HTML），异常压缩包整体拒绝且不产生任何残留。
"""
import os
import stat
import zipfile

from app.domain.errors import ArchiveRejectedError

# 单条目解压后大小上限（字节）：与供应方输入文件上限同量级
MAX_ENTRY_BYTES = 200 * 1024 * 1024
# 全部条目解压后总量上限（字节）：图片集合的宽松余量
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
# 条目数量上限：正常产物为固定结构的几十个文件
MAX_ENTRY_COUNT = 500
# 压缩比上限（解压后/压缩后）：超过即视为炸弹；
# 只对压缩后足够大的条目生效，避免小文件误报
MAX_COMPRESSION_RATIO = 100
_RATIO_CHECK_MIN_COMPRESSED_BYTES = 4096

# 产物扩展名白名单：Markdown/结构化 JSON/页面图片/HTML 正文
ALLOWED_EXTENSIONS = frozenset({".md", ".json", ".jpg", ".jpeg", ".png", ".html"})

# ZIP 外部属性中的文件类型位段：非常规文件/目录一律拒绝
# （符号链接、字符/块设备等类型位不匹配即拒绝）
_TYPE_MASK = 0o170000

# 流式解压的块大小（字节）
_EXTRACT_CHUNK_BYTES = 1024 * 1024


def extract_archive(zip_path: str, dest_dir: str) -> list[str]:
    """把结果压缩包安全解压到目标目录

    校验失败时整体拒绝，不写入任何文件（先校验后解压）。

    :param zip_path: 压缩包路径
    :param dest_dir: 解压目标目录（不存在时自动创建）
    :return: 解压出的相对路径列表（正斜杠分隔，按压缩包顺序）
    :raises ArchiveRejectedError: 任一安全校验未通过
    """
    with zipfile.ZipFile(zip_path) as archive:
        # 供应方随包回传原始输入副本（*_origin.<ext>）：它不是解析内容，
        # 跳过不落盘；其余条目仍严格执行白名单
        entries = [
            info
            for info in archive.infolist()
            if not info.is_dir() and not _is_origin_echo(info.filename)
        ]
        _validate_entries(entries)

        os.makedirs(dest_dir, exist_ok=True)
        extracted: list[str] = []
        for info in entries:
            relative = _safe_relative_path(info.filename)
            target = os.path.abspath(os.path.join(dest_dir, *relative.split("/")))
            if not target.startswith(os.path.abspath(dest_dir) + os.sep):
                raise ArchiveRejectedError("条目路径逃出解压目标目录")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as output:
                while block := source.read(_EXTRACT_CHUNK_BYTES):
                    output.write(block)
            extracted.append(relative)
        return extracted


def _is_origin_echo(entry_name: str) -> bool:
    """判断条目是否为供应方回传的原始输入副本（<hash>_origin.<ext>）

    :param entry_name: 压缩包内条目名
    :return: 是原始输入副本时为 True
    """
    base = entry_name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, _ = base.rpartition(".")
    return bool(dot) and stem.endswith("_origin")


def _validate_entries(entries: list) -> None:
    """对全部条目执行静态安全校验（不写盘）

    :param entries: 非目录条目列表
    :raises ArchiveRejectedError: 任一条目未通过校验
    """
    if len(entries) > MAX_ENTRY_COUNT:
        raise ArchiveRejectedError("压缩包含有的条目数量超过上限")
    total_bytes = 0
    for info in entries:
        _reject_link_or_device(info)
        _safe_relative_path(info.filename)
        if info.file_size > MAX_ENTRY_BYTES:
            raise ArchiveRejectedError("单条目解压后大小超过上限")
        total_bytes += info.file_size
        if total_bytes > MAX_TOTAL_BYTES:
            raise ArchiveRejectedError("压缩包解压后总量超过上限")
        if (
            info.compress_size >= _RATIO_CHECK_MIN_COMPRESSED_BYTES
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise ArchiveRejectedError("压缩比异常，疑似炸弹压缩包")
        if not _allowed_extension(info.filename):
            raise ArchiveRejectedError("条目类型不在允许列表")


def _reject_link_or_device(info) -> None:
    """拒绝符号链接、硬链接与设备类型条目

    :param info: 压缩包条目
    :raises ArchiveRejectedError: 条目不是常规文件/目录
    """
    mode = (info.external_attr >> 16) & _TYPE_MASK
    if mode not in (0, stat.S_IFREG):
        raise ArchiveRejectedError("条目不是常规文件")


def _safe_relative_path(entry_name: str) -> str:
    """校验并归一化条目路径，拒绝穿越与绝对路径

    :param entry_name: 压缩包内条目名
    :return: 归一化后的相对路径（正斜杠分隔）
    :raises ArchiveRejectedError: 路径为绝对路径或包含穿越段
    """
    name = entry_name.replace("\\", "/")
    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        raise ArchiveRejectedError("条目路径为绝对路径")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ArchiveRejectedError("条目路径包含非法段")
    return "/".join(parts)


def _allowed_extension(entry_name: str) -> bool:
    """判断条目扩展名是否在白名单

    :param entry_name: 压缩包内条目名
    :return: 允许时为 True
    """
    _, ext = os.path.splitext(entry_name.lower())
    return ext in ALLOWED_EXTENSIONS
