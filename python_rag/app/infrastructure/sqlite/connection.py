# -*- coding: utf-8 -*-
"""
SQLite 连接工厂

统一设置连接级 PRAGMA（WAL、外键、busy_timeout 等），避免各调用点
自行配置导致行为不一致：

    PRAGMA foreign_keys = ON;
    PRAGMA journal_mode = WAL;
    PRAGMA synchronous = NORMAL;
    PRAGMA busy_timeout = 5000;

所有需要访问业务 SQLite 的模块（迁移、Repository、Worker）都必须
通过本工厂创建连接，禁止绕过 PRAGMA 直接 sqlite3.connect。
"""
import sqlite3

# 默认 busy_timeout（毫秒）：SQLite 内部等待写锁的时间上限
BUSY_TIMEOUT_MS = 5000


def connect(db_path: str, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    """创建按规范配置好 PRAGMA 的 SQLite 连接

    :param db_path: 数据库文件路径（不存在时由 SQLite 自动创建）
    :param busy_timeout_ms: 可选的 busy_timeout 覆盖值（测试与特殊场景使用，
                            缺省使用 BUSY_TIMEOUT_MS）
    :return: autocommit 模式（isolation_level=None）的连接，
             事务边界由调用方通过显式 BEGIN/COMMIT 控制
    """
    timeout = BUSY_TIMEOUT_MS if busy_timeout_ms is None else busy_timeout_ms
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=timeout / 1000.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = %d;" % timeout)  # noqa: UP031 - PRAGMA 语句模板
    return conn
