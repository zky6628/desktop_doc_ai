# -*- coding: utf-8 -*-
"""
SQLite migration runner

设计约定：
- 迁移文件位于 python_rag/migrations/，命名为 NNNN_name.sql，
  版本号 NNNN 从 0001 起必须连续无缺口、无重复。
- 每个迁移文件在单个事务（BEGIN IMMEDIATE ... COMMIT）内应用，
  schema_migrations 记录与 DDL 同事务写入，保证原子性；
  应用失败时整体回滚、不写记录，修复后可直接重放。
- schema_migrations(version, name, applied_at, checksum) 记录版本、
  名称、应用时间与文件内容 SHA-256；已应用文件内容变化（checksum
  不一致）时拒绝继续迁移，防止静默改写历史 schema。
- 重复运行幂等：已应用版本自动跳过。
- 不提供 down 迁移：恢复手段为备份回档后重放。
- SQLite busy/locked 在本层毫秒级短重试，不与上层业务的重试计数
  耦合；短重试耗尽后才转为数据库错误。

限制：迁移 SQL 内禁止再写 BEGIN/COMMIT/END 语句控制事务，
事务边界统一由本 runner 管理。
"""
import datetime
import hashlib
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass

from .connection import connect
from .transactions import is_busy_error, rollback_quietly

# schema_migrations 基建表：version/name/applied_at/checksum 四字段
SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);
"""

# 迁移文件名格式：4 位数字版本号 + 下划线 + 小写字母/数字/下划线名称
_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

# 迁移 SQL 中禁止出现的显式事务控制语句（语句起始位置匹配，避免误伤正文字符串）
_FORBIDDEN_TXN_RE = re.compile(r"^\s*(BEGIN|COMMIT|END|ROLLBACK)\b", re.MULTILINE | re.IGNORECASE)

# busy/locked 毫秒级短重试：busy_timeout 耗尽后再退避重试的次数与基础间隔（秒）
_SHORT_RETRY_ATTEMPTS = 3
_SHORT_RETRY_BACKOFF_SECONDS = 0.05


class MigrationError(RuntimeError):
    """迁移流程错误：文件命名/顺序/校验和不一致或 SQL 应用失败"""


@dataclass(frozen=True)
class MigrationFile:
    """单个迁移文件解析结果"""

    version: int  # 版本号（来自文件名 NNNN）
    name: str  # 迁移名称（来自文件名 name 部分）
    path: str  # 文件绝对路径
    checksum: str  # 文件内容 SHA-256（hex）
    sql: str  # 文件原始 SQL 内容


def discover_migrations(migrations_dir: str) -> list:
    """扫描迁移目录并做静态校验

    :param migrations_dir: 迁移文件目录
    :return: 按 version 升序的 MigrationFile 列表
    :raises MigrationError: 文件名不合法、版本重复或有缺口
    """
    entries = []
    for filename in sorted(os.listdir(migrations_dir)):
        match = _FILENAME_RE.match(filename)
        if match is None:
            # 忽略非迁移文件（如 .gitkeep），但形如 NNNN_*.sql 却不合法的必须报错
            if re.match(r"^\d", filename):
                raise MigrationError(f"非法迁移文件名: {filename}（应为 NNNN_name.sql）")
            continue
        path = os.path.join(migrations_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        entries.append(
            MigrationFile(
                version=int(match.group(1)),
                name=match.group(2),
                path=path,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )

    entries.sort(key=lambda entry: entry.version)

    # 版本必须从 1 开始连续无缺口（enumerate 从 1 计数，重复或缺口都会失配）
    for expected_version, entry in enumerate(entries, start=1):
        if entry.version != expected_version:
            raise MigrationError(
                f"迁移版本不连续: 期望 {expected_version:04d}，"
                f"实际 {entry.version:04d}（{entry.name}）"
            )
    return entries


def load_applied(conn: sqlite3.Connection) -> dict:
    """读取已应用的迁移记录

    :param conn: 数据库连接
    :return: {version: (name, checksum)} 字典
    """
    rows = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(version): (name, checksum) for version, name, checksum in rows}


def apply_migrations(db_path: str, migrations_dir: str) -> int:
    """把 migrations_dir 下所有未应用迁移按顺序应用到 db_path

    :param db_path: 目标 SQLite 数据库文件路径
    :param migrations_dir: 迁移文件目录
    :return: 本次实际新应用的迁移数量（幂等重复运行返回 0）
    :raises MigrationError: 顺序、校验和、文件缺失或 SQL 执行失败
    """
    conn = connect(db_path)
    try:
        # 基建表先于一切迁移存在，不属于业务 schema 版本；
        # 建表同样在 busy 短重试保护下执行
        _execute_script_with_busy_retry(conn, SCHEMA_MIGRATIONS_DDL, "schema_migrations 基建表")
        applied = load_applied(conn)
        files = discover_migrations(migrations_dir)

        # 已应用版本对应的文件必须仍然存在，否则无法判断 schema 真实状态
        missing = sorted(set(applied) - {entry.version for entry in files})
        if missing:
            raise MigrationError(
                "已应用迁移的文件缺失: {}".format(
                    ", ".join(f"{version:04d}" for version in missing)
                )
            )

        # 已应用文件内容不得变更（checksum 防篡改）
        for entry in files:
            if entry.version in applied and applied[entry.version][1] != entry.checksum:
                raise MigrationError(
                    f"迁移 {entry.version:04d}_{entry.name} "
                    "内容与应用时不一致（checksum 不匹配），拒绝继续"
                )

        applied_count = 0
        for entry in files:
            if entry.version in applied:
                continue
            _apply_one(conn, entry)
            applied_count += 1
        return applied_count
    finally:
        conn.close()


def _apply_one(conn: sqlite3.Connection, entry: MigrationFile) -> None:
    """在单事务内应用一个迁移并写入 schema_migrations 记录

    :param conn: 数据库连接（autocommit 模式，事务由脚本内 BEGIN/COMMIT 控制）
    :param entry: 待应用的迁移文件
    :raises MigrationError: SQL 失败（busy 短重试耗尽后抛出）
    """
    if _FORBIDDEN_TXN_RE.search(entry.sql):
        raise MigrationError(
            f"迁移 {entry.version:04d}_{entry.name} "
            "含显式事务控制语句（BEGIN/COMMIT/END/ROLLBACK），事务边界由 runner 统一管理"
        )

    applied_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    # name 来自文件名正则（[a-z0-9_]+），checksum 为 hex，applied_at 为 isoformat，
    # 三者均不可能含单引号；此处仍做标准转义以保持防御性
    record_sql = (
        "INSERT INTO schema_migrations(version, name, applied_at, checksum) "
        f"VALUES ({entry.version}, '{entry.name.replace("'", "''")}', "
        f"'{applied_at}', '{entry.checksum}');"
    )
    # executescript 会先隐式提交挂起事务，因此 BEGIN 必须放进脚本内，
    # 使 DDL 与 schema_migrations 记录处于同一原子事务
    script = f"BEGIN IMMEDIATE;\n{entry.sql.strip()}\n{record_sql}\nCOMMIT;"
    _execute_script_with_busy_retry(
        conn, script, f"迁移 {entry.version:04d}_{entry.name}"
    )


def _execute_script_with_busy_retry(
    conn: sqlite3.Connection, script: str, label: str
) -> None:
    """执行 SQL 脚本，busy/locked 时毫秒级退避短重试

    短重试仅针对 SQLite busy/locked 类错误，与上层业务的重试计数无关；
    其他错误立即抛出。

    :param conn: 数据库连接
    :param script: 完整 SQL 脚本（可含事务控制）
    :param label: 错误信息中使用的目标描述
    :raises MigrationError: 重试耗尽或非 busy 错误
    """
    for attempt in range(1, _SHORT_RETRY_ATTEMPTS + 1):
        try:
            conn.executescript(script)
            return
        except sqlite3.OperationalError as exc:
            rollback_quietly(conn)
            if is_busy_error(exc) and attempt < _SHORT_RETRY_ATTEMPTS:
                time.sleep(_SHORT_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise MigrationError(f"{label} 应用失败: {exc}") from exc


# 本文件位于 python_rag/app/infrastructure/sqlite/，向上三级即 python_rag/
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(_MODULE_DIR))), "migrations"
)


def main(argv: list | None = None) -> int:
    """命令行入口：对指定数据库应用全部未应用迁移

    用法：python -m app.infrastructure.sqlite.migrations <db_path>
    迁移目录默认为仓库内 python_rag/migrations/。
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("用法: python -m app.infrastructure.sqlite.migrations <db_path>", file=sys.stderr)
        return 2

    applied_count = apply_migrations(argv[0], _DEFAULT_MIGRATIONS_DIR)
    print(f"迁移完成: 本次应用 {applied_count} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
