# -*- coding: utf-8 -*-
"""UTC 时间工具

时间戳字段统一存储 UTC ISO-8601 文本。
"""
from datetime import datetime, timezone


def utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO-8601 文本（秒精度）

    :return: 形如 2026-09-17T09:30:00+00:00 的字符串
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
