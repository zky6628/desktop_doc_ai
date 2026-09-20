# -*- coding: utf-8 -*-
"""keyset 分页工具：游标编码与页大小约束

排序键统一为 (created_at, id) 倒序；游标为页尾排序键的 base64
编码，服务端解码后作为下一页的排他下界。游标非法按参数错误表达，
不做静默重置（避免客户端重复拉到同一页）。
"""
import base64
import binascii


class InvalidCursorError(ValueError):
    """游标格式非法（调用方按 INVALID_PARAM 处理）"""


def encode_cursor(created_at: str, entity_id: str) -> str:
    """把页尾排序键编码为不透明游标"""
    raw = f"{created_at}|{entity_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """解码游标为 (created_at, id) 排序键

    :raises InvalidCursorError: 游标不是合法编码或结构不符
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursorError("游标格式非法") from exc
    parts = raw.split("|", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise InvalidCursorError("游标结构不符")
    return parts[0], parts[1]


def clamp_limit(value: int | None, default: int = 50, maximum: int = 200) -> int:
    """约束页大小：未提供用默认值，超出上限截到上限，非正数按参数错误"""
    if value is None:
        return default
    if value <= 0:
        raise InvalidCursorError("页大小必须为正整数")
    return min(value, maximum)
