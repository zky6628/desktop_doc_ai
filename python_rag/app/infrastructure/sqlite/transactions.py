# -*- coding: utf-8 -*-
"""
SQLite 事务执行器

为 Repository Adapter 提供统一的事务边界与 busy 短重试：
每次仓储操作在 BEGIN IMMEDIATE ... COMMIT 内完成，失败整体回滚；
SQLite busy/locked 在本层毫秒级退避短重试，与上层业务的重试计数
无关，短重试耗尽后才转为 RepositoryError。
"""
import sqlite3
import time

from app.domain.errors import RepositoryError

# busy/locked 短重试：busy_timeout 耗尽后的退避重试次数与基础间隔（秒）
_SHORT_RETRY_ATTEMPTS = 3
_SHORT_RETRY_BACKOFF_SECONDS = 0.05


def is_busy_error(exc: sqlite3.OperationalError) -> bool:
    """判断异常是否为 SQLite busy/locked 类错误

    :param exc: sqlite3 异常
    :return: 是 busy/locked 时为 True
    """
    message = str(exc).lower()
    return "busy" in message or "locked" in message


def rollback_quietly(conn: sqlite3.Connection) -> None:
    """尽力回滚当前连接上的残留事务（无事务或回滚失败时忽略）"""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


def run_in_transaction(conn: sqlite3.Connection, fn, label: str):
    """在单事务内执行仓储操作

    :param conn: 数据库连接（autocommit 模式）
    :param fn: 接收 conn 的操作函数；抛出的领域错误保持原样向上传播
    :param label: 错误信息中使用的操作描述
    :return: fn 的返回值
    :raises RepositoryError: busy 短重试耗尽或其他 SQL 错误
    """
    for attempt in range(1, _SHORT_RETRY_ATTEMPTS + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except sqlite3.OperationalError as exc:
            rollback_quietly(conn)
            if is_busy_error(exc) and attempt < _SHORT_RETRY_ATTEMPTS:
                time.sleep(_SHORT_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise RepositoryError(f"{label} 失败: {exc}") from exc
        except Exception:
            rollback_quietly(conn)
            raise
    raise RepositoryError(f"{label} 失败: 重试耗尽")  # pragma: no cover - 防御分支
