# -*- coding: utf-8 -*-
"""证据包收集：验收报告所需的环境与版本事实一次汇出

收集项对齐证据包清单中可自动化的部分：后端版本（git 提交）、依赖
锁哈希、schema 迁移版本、在役配置行（含哈希）与模型身份（经运行
配置端点，不直读库）、Python/Flutter 版本。网络环境与人工项输出
空模板由验收人填写。

用法：
  python collect_evidence.py [--base http://127.0.0.1:8000]
                             [--db data/workbench.db] [--out evidence.json]
"""
import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def _tool_version(command: list[str]) -> str | None:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip().splitlines()[0]
    except (subprocess.SubprocessError, OSError, IndexError):
        return None


def collect(db_path: str, base: str) -> dict:
    schema_version = None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        schema_version = row[0] if row else None
    finally:
        conn.close()

    import httpx

    with httpx.Client(base_url=base, timeout=30) as client:
        config = client.get("/api/v1/config/public").json()["data"]

    return {
        "collected_at": _tool_version(["python", "-c", "import datetime; print(datetime.datetime.now(datetime.UTC).isoformat())"]),
        "backend_git_commit": _git("rev-parse", "HEAD"),
        "backend_git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "flutter_version": _tool_version(["flutter", "--version", "--machine"]) and _tool_version(["flutter", "--version"]),
        "python_version": _tool_version(["python", "--version"]),
        "dependency_locks": {
            "python_requirements_lock": _sha256(BASE_DIR / "requirements.lock"),
            "python_requirements_dev_lock": _sha256(BASE_DIR / "requirements-dev.lock"),
            "flutter_pubspec_lock": _sha256(BASE_DIR.parent / "flutter_app" / "pubspec.lock"),
        },
        "schema_migration_version": schema_version,
        "model_profiles": config.get("model_profiles"),
        "active_pipeline_configs": config.get("pipeline_configs"),
        "limits": config.get("limits"),
        "features": config.get("features"),
        "network_environment": {
            "carrier": None,
            "bandwidth_up_down": None,
            "avg_latency_ms": None,
            "packet_loss": None,
            "connection_type": None,
            "dashscope_region": None,
            "tested_at": None,
            "note": "验收人按验收规范网络记录要求填写",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="收集验收证据包环境事实")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "workbench.db"), help="工作台 DB 路径")
    parser.add_argument("--out", default="evidence-manifest.json", help="输出 JSON 路径")
    args = parser.parse_args(argv)

    try:
        evidence = collect(args.db, args.base)
    except (OSError, sqlite3.Error) as exc:
        print(f"收集失败: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    print(f"证据清单已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
