# -*- coding: utf-8 -*-
"""
migration runner 单元测试：

- 全新 SQLite 文件可按 migration 顺序创建
- 升级 migration 可重复检测且不可重复应用
- 失败回滚可重放、checksum 防篡改、缺口检测、busy 短重试
"""
import sqlite3
import threading
import time

import pytest

from app.infrastructure.sqlite import migrations as migrations_mod
from app.infrastructure.sqlite.connection import BUSY_TIMEOUT_MS, connect
from app.infrastructure.sqlite.migrations import (
    MigrationError,
    apply_migrations,
    discover_migrations,
)


def _write_migration(directory, version, name, sql):
    """在目录中写入一个迁移文件（测试辅助）"""
    path = directory / f"{version:04d}_{name}.sql"
    path.write_text(sql, encoding="utf-8")
    return path


def _migration_rows(conn):
    """读取 schema_migrations 全部记录（按版本排序）"""
    return conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()


def test_empty_database_applies_in_order(tmp_path):
    """验收：全新 SQLite 文件可按 migration 顺序创建"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_migration(migrations_dir, 1, "config_tables", "CREATE TABLE t1 (id INTEGER PRIMARY KEY);")
    _write_migration(migrations_dir, 2, "kb_documents", "CREATE TABLE t2 (id INTEGER PRIMARY KEY);")
    db_path = tmp_path / "app.db"

    applied_count = apply_migrations(str(db_path), str(migrations_dir))

    assert applied_count == 2
    conn = connect(str(db_path))
    try:
        rows = _migration_rows(conn)
        assert [row[0] for row in rows] == [1, 2]
        # 表确实被创建
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"t1", "t2", "schema_migrations"} <= tables
    finally:
        conn.close()


def test_rerun_is_idempotent(tmp_path):
    """验收：migration 可重复检测且不可重复应用（重复运行返回 0 且记录不变）"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_migration(migrations_dir, 1, "first", "CREATE TABLE t1 (id INTEGER PRIMARY KEY);")
    db_path = str(tmp_path / "app.db")

    assert apply_migrations(db_path, str(migrations_dir)) == 1

    conn = connect(db_path)
    rows_before = _migration_rows(conn)
    conn.close()

    assert apply_migrations(db_path, str(migrations_dir)) == 0

    conn = connect(db_path)
    try:
        assert _migration_rows(conn) == rows_before
    finally:
        conn.close()


def test_failed_migration_rolls_back_and_can_retry(tmp_path):
    """失败整体回滚、不写记录，修复后可直接重放"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_migration(migrations_dir, 1, "good", "CREATE TABLE t1 (id INTEGER PRIMARY KEY);")
    _write_migration(migrations_dir, 2, "bad", "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\nINSERT INTO nonexistent VALUES (1);")
    db_path = str(tmp_path / "app.db")

    with pytest.raises(MigrationError):
        apply_migrations(db_path, str(migrations_dir))

    conn = connect(db_path)
    try:
        # 迁移 2 失败：其 DDL 与记录都不存在；迁移 1 保留
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "t1" in tables and "t2" not in tables
        assert [row[0] for row in _migration_rows(conn)] == [1]
    finally:
        conn.close()

    # 修复坏迁移后重放成功
    _write_migration(migrations_dir, 2, "bad", "CREATE TABLE t2 (id INTEGER PRIMARY KEY);")
    assert apply_migrations(db_path, str(migrations_dir)) == 1

    conn = connect(db_path)
    try:
        assert [row[0] for row in _migration_rows(conn)] == [1, 2]
    finally:
        conn.close()


def test_checksum_mismatch_rejected(tmp_path):
    """已应用迁移文件内容变更（checksum 不匹配）时拒绝继续"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    path = _write_migration(migrations_dir, 1, "first", "CREATE TABLE t1 (id INTEGER PRIMARY KEY);")
    db_path = str(tmp_path / "app.db")
    apply_migrations(db_path, str(migrations_dir))

    # 篡改已应用文件内容
    path.write_text("CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n-- tampered\n", encoding="utf-8")

    with pytest.raises(MigrationError, match="checksum"):
        apply_migrations(db_path, str(migrations_dir))


def test_version_gap_detected(tmp_path):
    """版本必须从 0001 起连续：缺口（0001、0003）时报错"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_migration(migrations_dir, 1, "a", "CREATE TABLE a (id INTEGER PRIMARY KEY);")
    _write_migration(migrations_dir, 3, "c", "CREATE TABLE c (id INTEGER PRIMARY KEY);")

    with pytest.raises(MigrationError, match="不连续"):
        discover_migrations(str(migrations_dir))


def test_missing_applied_file_detected(tmp_path):
    """已应用迁移的文件被删除时拒绝迁移（schema 状态不可判定）"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    path = _write_migration(migrations_dir, 1, "first", "CREATE TABLE t1 (id INTEGER PRIMARY KEY);")
    db_path = str(tmp_path / "app.db")
    apply_migrations(db_path, str(migrations_dir))

    path.unlink()

    with pytest.raises(MigrationError, match="缺失"):
        apply_migrations(db_path, str(migrations_dir))


def test_explicit_transaction_rejected(tmp_path):
    """迁移 SQL 内禁止显式事务控制（事务边界由 runner 统一管理）"""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_migration(migrations_dir, 1, "txn", "BEGIN;\nCREATE TABLE t1 (id INTEGER PRIMARY KEY);\nCOMMIT;")

    with pytest.raises(MigrationError, match="事务"):
        apply_migrations(str(tmp_path / "app.db"), str(migrations_dir))


def test_busy_short_retry_succeeds(tmp_path, monkeypatch):
    """busy/locked 毫秒级短重试，重试后迁移完整应用且记录写入"""
    # 用极小 busy_timeout 让短重试快速触发（50ms 耗尽 -> 退避重试），
    # 并缩短退避间隔、提高重试次数，避免测试真实等待数秒
    monkeypatch.setattr(
        migrations_mod, "connect", lambda path: connect(path, busy_timeout_ms=50)
    )
    monkeypatch.setattr(migrations_mod, "_SHORT_RETRY_ATTEMPTS", 8)
    monkeypatch.setattr(migrations_mod, "_SHORT_RETRY_BACKOFF_SECONDS", 0.02)

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_migration(migrations_dir, 1, "contended", "CREATE TABLE t1 (id INTEGER PRIMARY KEY);")
    db_path = str(tmp_path / "app.db")

    # 持锁、释放都在主线程（SQLite 连接不可跨线程使用）；
    # runner 移入子线程：先启动 runner 让其撞锁进入短重试，
    # 主线程 200ms 后自行 COMMIT 释放写锁
    blocker = connect(db_path)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("CREATE TABLE blocker_keepalive (id INTEGER)")

        result = {}

        def _run_migrations():
            try:
                result["applied"] = apply_migrations(db_path, str(migrations_dir))
            except Exception as exc:  # noqa: BLE001 - 测试断言需跨线程传播任意异常
                result["error"] = exc

        worker = threading.Thread(target=_run_migrations)
        worker.start()
        time.sleep(0.2)
        blocker.execute("COMMIT")
        worker.join(timeout=10)

        assert "error" not in result, "runner 失败: {!r}".format(result.get("error"))
        assert result["applied"] == 1
    finally:
        blocker.close()

    conn = connect(db_path)
    try:
        assert [row[0] for row in _migration_rows(conn)] == [1]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "t1" in tables
    finally:
        conn.close()


def test_connection_pragmas(tmp_path):
    """连接工厂必须执行四项 PRAGMA"""
    db_path = str(tmp_path / "app.db")
    conn = connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert conn.isolation_level is None  # 显式事务控制
    finally:
        conn.close()


def test_foreign_keys_enforced(tmp_path):
    """外键约束在连接上真实生效"""
    db_path = str(tmp_path / "app.db")
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (pid INTEGER REFERENCES parent(id))")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO child (pid) VALUES (999)")
    finally:
        conn.close()
