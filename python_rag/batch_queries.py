# -*- coding: utf-8 -*-
"""批量跑题脚本：问题集逐条经真实查询链路执行并汇总 TTFT 事实

M7 的 100 次 TTFT 报告由客户端口径（应用内评测模式）产出，本脚本
产出服务端口径的分段事实（检索/重排/Prompt/模型首 token）与查询
状态分布，兼作检索质量评测（Recall/MRR/nDCG）的跑题工具——候选与
引用明细经 export_evaluations.py 导出后由 compute_metrics.py 计算。

只触发查询链路，不触发重建与嵌入调用（与评测编排解耦）；问题逐
条串行执行（每条等待终态再发下一条），保证延迟数据互不干扰。

用法：
  python batch_queries.py --kb <kb_id> --questions questions.txt
                          [--base http://127.0.0.1:8000]
                          [--out results.jsonl]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

# 单题终态轮询上限（秒）：生成卡死兜底
_QUERY_TIMEOUT_SECONDS = 600
_POLL_INTERVAL_SECONDS = 0.2

# 查询终态（与查询生命周期合同一致）
_TERMINAL_STATES = {"completed", "failed", "cancelled"}


def load_questions(path: str) -> list[str]:
    """问题集：文本文件每行一问；空白行与重复行忽略"""
    questions = []
    seen = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        question = line.strip()
        if not question or question in seen:
            continue
        seen.add(question)
        questions.append(question)
    return questions


def run_one(client: httpx.Client, kb_id: str, question: str, index: int) -> dict:
    """执行单个问题至终态，返回该题的事实行"""
    started = time.monotonic()
    created = client.post(
        "/api/v1/queries",
        json={"knowledge_base_id": kb_id, "question": question},
    )
    if created.status_code != 202:
        return {
            "index": index,
            "question": question,
            "state": "submit_failed",
            "http_status": created.status_code,
            "error": created.text[:200],
        }
    query_id = created.json()["data"]["query_id"]
    deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
    while True:
        detail = client.get(f"/api/v1/queries/{query_id}")
        if detail.status_code != 200:
            return {
                "index": index,
                "question": question,
                "query_id": query_id,
                "state": "poll_failed",
                "http_status": detail.status_code,
            }
        data = detail.json()["data"]
        if data.get("state") in _TERMINAL_STATES:
            break
        if time.monotonic() > deadline:
            return {
                "index": index,
                "question": question,
                "query_id": query_id,
                "state": "timeout",
            }
        time.sleep(_POLL_INTERVAL_SECONDS)
    return {
        "index": index,
        "question": question,
        "query_id": query_id,
        "state": data["state"],
        "refused": data.get("refused"),
        "rerank_degraded": data.get("rerank_degraded"),
        "retrieval_ms": data.get("retrieval_ms"),
        "rerank_ms": data.get("rerank_ms"),
        "prompt_build_ms": data.get("prompt_build_ms"),
        "model_ttft_ms": data.get("model_ttft_ms"),
        "server_ttft_ms": data.get("server_ttft_ms"),
        "total_ms": data.get("total_ms"),
        "input_tokens": data.get("input_tokens"),
        "output_tokens": data.get("output_tokens"),
        "error_code": data.get("error_code"),
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def summarize(rows: list[dict]) -> dict:
    """服务端 TTFT 汇总（成功请求口径；失败不入分位）"""
    succeeded = [row for row in rows if row.get("state") == "completed"]
    ttfts = sorted(
        row["server_ttft_ms"] for row in succeeded if row.get("server_ttft_ms") is not None
    )

    def percentile(values: list[int], ratio: float) -> int | None:
        if not values:
            return None
        return values[min(round(ratio * (len(values) - 1)), len(values) - 1)]

    return {
        "total": len(rows),
        "completed": len(succeeded),
        "failed": sum(1 for row in rows if row.get("state") == "failed"),
        "cancelled": sum(1 for row in rows if row.get("state") == "cancelled"),
        "refused": sum(1 for row in rows if row.get("refused")),
        "submit_or_poll_failed": sum(
            1 for row in rows if row.get("state") in ("submit_failed", "poll_failed", "timeout")
        ),
        "server_ttft_p50_ms": percentile(ttfts, 0.50),
        "server_ttft_p95_ms": percentile(ttfts, 0.95),
        "server_ttft_p99_ms": percentile(ttfts, 0.99),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量跑题：串行执行问题集并汇总")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    parser.add_argument("--kb", required=True, help="知识库 ID")
    parser.add_argument("--questions", required=True, help="问题集文件（每行一问）")
    parser.add_argument("--out", default=None, help="结果 JSONL 路径（缺省 stdout 摘要）")
    args = parser.parse_args(argv)

    questions = load_questions(args.questions)
    if not questions:
        print("问题集为空", file=sys.stderr)
        return 2
    print(f"共 {len(questions)} 题，逐条串行执行…", file=sys.stderr)

    rows = []
    with httpx.Client(base_url=args.base, timeout=30) as client:
        for index, question in enumerate(questions, start=1):
            row = run_one(client, args.kb, question, index)
            rows.append(row)
            print(
                f"[{index}/{len(questions)}] {row['state']}"
                + (f" ttft={row.get('server_ttft_ms')}ms" if row.get("server_ttft_ms") else ""),
                file=sys.stderr,
            )

    summary = summarize(rows)
    if args.out:
        with Path(args.out).open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"明细已写入 {args.out}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["completed"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
