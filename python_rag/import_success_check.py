# -*- coding: utf-8 -*-
"""导入成功率验收脚本：按数据集 manifest 批量上传并统计成功率

口径对齐验收硬指标：成功率 = 应成功样本中任务终态 succeeded 的比
例，分母只含"支持格式且未损坏"的样本（manifest 中 expect_success
为真）；安全拒绝/损坏/加密样本（expect_success 为假）单独核对预期
拒绝行为，不进成功率分母——既不抬高也不拉低。

manifest 为 CSV（UTF-8，首行表头）：
  file,expect_success,note
  样例：sample.pdf,true,文本型 PDF / broken.docx,false,损坏样本

用法：
  python import_success_check.py --kb <kb_id> --manifest manifest.csv
                                 [--base http://127.0.0.1:8000]
                                 [--out report.json]
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

import httpx

# 单任务终态轮询上限（秒）：云端解析路线可能分钟级
_TASK_TIMEOUT_SECONDS = 900
_POLL_INTERVAL_SECONDS = 1.0


def load_manifest(path: str) -> list[dict]:
    rows = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            file = (row.get("file") or "").strip()
            if not file:
                continue
            rows.append(
                {
                    "file": file,
                    "expect_success": (row.get("expect_success") or "").strip().lower()
                    in ("true", "1", "yes"),
                    "note": (row.get("note") or "").strip(),
                }
            )
    return rows


def wait_task_terminal(client: httpx.Client, task_id: str) -> dict:
    deadline = time.monotonic() + _TASK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/tasks/{task_id}")
        if detail.status_code == 200:
            # 任务详情结构：{recent_events, task}——状态在嵌套 task 对象
            task = detail.json()["data"].get("task") or {}
            state = task.get("state")
            if state in ("succeeded", "failed", "cancelled"):
                return task
        time.sleep(_POLL_INTERVAL_SECONDS)
    return {"state": "timeout"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入成功率批量验收")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    parser.add_argument("--kb", required=True, help="目标知识库 ID")
    parser.add_argument("--manifest", required=True, help="manifest CSV 路径")
    parser.add_argument("--out", default=None, help="报告 JSON 路径")
    args = parser.parse_args(argv)

    entries = load_manifest(args.manifest)
    if not entries:
        print("manifest 为空", file=sys.stderr)
        return 2
    print(f"共 {len(entries)} 个样本", file=sys.stderr)

    results = []
    with httpx.Client(base_url=args.base, timeout=120) as client:
        for index, entry in enumerate(entries, start=1):
            file_path = Path(entry["file"])
            if not file_path.exists():
                results.append({**entry, "outcome": "manifest_file_missing"})
                print(f"[{index}/{len(entries)}] {entry['file']} 文件不存在", file=sys.stderr)
                continue
            with file_path.open("rb") as handle:
                response = client.post(
                    f"/api/v1/knowledge-bases/{args.kb}/documents",
                    files={"files": (file_path.name, handle)},
                    data={"parser_preference": "auto", "duplicate_policy": "new_version"},
                )
            if response.status_code != 202:
                results.append(
                    {**entry, "outcome": "submit_rejected", "http_status": response.status_code}
                )
                print(f"[{index}/{len(entries)}] {entry['file']} 上传被拒 {response.status_code}", file=sys.stderr)
                continue
            payload = response.json()["data"]["results"]
            file_result = next(
                (item for item in payload if item.get("display_name") == file_path.name),
                payload[0] if payload else None,
            )
            if file_result is None or not file_result.get("accepted"):
                results.append(
                    {
                        **entry,
                        "outcome": "rejected_at_submit",
                        "error": (file_result or {}).get("error"),
                    }
                )
                print(f"[{index}/{len(entries)}] {entry['file']} 提交即拒", file=sys.stderr)
                continue
            task = wait_task_terminal(client, file_result["task_id"])
            outcome = "succeeded" if task.get("state") == "succeeded" else task.get("state")
            results.append(
                {
                    **entry,
                    "outcome": outcome,
                    "task_state": task.get("state"),
                    "error_code": task.get("error_code"),
                }
            )
            print(f"[{index}/{len(entries)}] {entry['file']} → {outcome}", file=sys.stderr)

    # 成功率口径：分母只含应成功样本（支持格式且未损坏）
    should_success = [row for row in results if row["expect_success"]]
    succeeded = [row for row in should_success if row.get("outcome") == "succeeded"]
    should_fail = [row for row in results if not row["expect_success"]]
    correctly_rejected = [
        row for row in should_fail if row.get("outcome") in ("rejected_at_submit", "failed")
    ]
    report = {
        "total_samples": len(results),
        "success_expected": len(should_success),
        "success_actual": len(succeeded),
        "success_rate": round(len(succeeded) / len(should_success), 4) if should_success else None,
        "target_rate": 0.98,
        "meets_target": bool(should_success)
        and len(succeeded) / len(should_success) >= 0.98,
        "failure_expected": len(should_fail),
        "correctly_rejected": len(correctly_rejected),
        "failures": [
            {"file": row["file"], "outcome": row.get("outcome"), "error_code": row.get("error_code")}
            for row in should_success
            if row.get("outcome") != "succeeded"
        ],
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps({"report": report, "details": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["meets_target"] else 1


if __name__ == "__main__":
    sys.exit(main())
