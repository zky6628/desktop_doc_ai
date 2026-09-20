# -*- coding: utf-8 -*-
"""上传文件读取工具：同步文件对象到字节块迭代器的包装

Starlette 暂存文件对象为同步读取接口，端点层按块消费以避免整文件
进入内存；块大小默认复用上传策略常量，保证口径一致。
"""
from collections.abc import Iterable

from app.domain import file_policy


def chunk_file_reader(
    fp, chunk_size: int = file_policy.STREAM_CHUNK_BYTES
) -> Iterable[bytes]:
    """把已定位到起始位置的二进制文件对象包装为按块读取的迭代器"""
    return iter(lambda: fp.read(chunk_size), b"")
