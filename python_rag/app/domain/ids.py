# -*- coding: utf-8 -*-
"""
UUIDv7 生成工具（RFC 9562）

业务资源 ID 统一使用无前缀、规范小写字符串形式的 UUIDv7；
时间戳字段统一 UTC ISO-8601 文本。

Python 3.12 标准库的 uuid 模块尚未提供 uuid7（3.14 起才有），
因此委托 PyPI 的 uuid6 库生成（纯标准库实现、无传递依赖、
Python 3.8+ 兼容），本模块只做输出格式约定与时间位段读取。
"""
import uuid

# PyPI uuid6 库：提供 uuid6/uuid7/uuid8，返回标准库 uuid.UUID 实例
from uuid6 import uuid7 as _uuid7

# unix_ts_ms 位段宽度与最大值（48 bit，RFC 9562）
_TS_BITS = 48
_TS_MASK = (1 << _TS_BITS) - 1


def uuid7() -> str:
    """生成一个 UUIDv7 规范小写字符串

    时间位取当前 UTC 毫秒时间戳，随机位由库内安全随机源填充；
    同毫秒内多次调用产生时间位相同但随机位不同的 ID。

    :return: 形如 019012af-...-... 的 36 字符规范小写 UUIDv7 字符串
    """
    return str(_uuid7())


def extract_timestamp_ms(identifier: str) -> int:
    """从 UUIDv7 字符串提取内嵌的 UTC 毫秒时间戳

    :param identifier: UUIDv7 规范字符串
    :return: unix 毫秒时间戳（RFC 9562 时间位段原文）
    :raises ValueError: 非法 UUID 或非 v7 版本
    """
    parsed = uuid.UUID(identifier)
    if parsed.version != 7:
        raise ValueError(f"非 UUIDv7（version nibble={parsed.version}）: {identifier}")
    return (parsed.int >> 80) & _TS_MASK
