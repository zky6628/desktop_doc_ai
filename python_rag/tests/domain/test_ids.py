# -*- coding: utf-8 -*-
"""UUIDv7 生成器测试（规范小写字符串形式）"""
import re
import uuid

from app.domain.ids import extract_timestamp_ms, uuid7

# 规范形式：8-4-4-4-12，version 位为 7，variant 位为 10（首字符 8/9/a/b）
_UUID7_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def test_uuid7_matches_rfc9562_layout():
    """生成值满足 RFC 9562 位布局：version=7、variant=10、规范小写"""
    value = uuid7()
    assert _UUID7_PATTERN.fullmatch(value), value


def test_uuid7_parseable_by_stdlib():
    """标准库可解析且识别为版本 7"""
    parsed = uuid.UUID(uuid7())
    assert parsed.version == 7


def test_uuid7_unique_over_many_samples():
    """大批量采样无重复（随机位熵足够）"""
    values = {uuid7() for _ in range(10_000)}
    assert len(values) == 10_000


def test_uuid7_timestamp_monotonic_nondecreasing():
    """时间位随真实时间单调不减（同毫秒内相等亦合法）"""
    previous = extract_timestamp_ms(uuid7())
    for _ in range(100):
        current = extract_timestamp_ms(uuid7())
        assert current >= previous
        previous = current


def test_extract_timestamp_rejects_non_v7():
    """非 v7 的 UUID 提取时间戳时报错"""
    import pytest

    with pytest.raises(ValueError):
        extract_timestamp_ms(str(uuid.uuid4()))
