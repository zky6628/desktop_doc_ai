# -*- coding: utf-8 -*-
"""系统设置仓储的 SQLite 实现：运行时可变键值的覆盖式写入

设置只存"当前选择"，不承载历史（历史事实由 pipeline_configs 版本
行表达）；写入即生效，读取端每次直查以获取最新值（无缓存，单线程
Worker 与 API 的并发读写由 SQLite 事务隔离保证）。
"""
from app.domain.clock import utc_now_iso
from app.domain.ports import SystemSettingsRepository as SystemSettingsRepositoryPort


class SQLiteSystemSettingsRepository(SystemSettingsRepositoryPort):
    """system_settings 表的读写实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value_json FROM system_settings WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row[0]

    def put(self, key: str, value_json: str) -> None:
        self._conn.execute(
            "INSERT INTO system_settings (key, value_json, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET"
            " value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, value_json, utc_now_iso()),
        )

    def delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM system_settings WHERE key = ?", (key,))
