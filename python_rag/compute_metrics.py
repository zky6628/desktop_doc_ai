# -*- coding: utf-8 -*-
"""评测指标计算：TTFT 分布、分段目标对照与检索质量（分层 qrels）

输入两类产物：
  1. batch_queries.py 的结果 JSONL（TTFT 与状态事实）
  2. export_evaluations.py 的导出 JSONL（候选明细，含 file_name）

qrels 为分层标注（JSONL，每行一题）：relevant_docs 必填（文档名列
表），sources 与 relevance 可选。计算口径：
  - Recall@5：候选按 rerank_rank（降级时 rrf_rank）取前 5，命中文档
    按相关文档集合去重
  - MRR@10：首个相关命中的排名倒数均值
  - nDCG@10：有分级用分级（缺省 1），DCG/IDCG 按 standard 折折减

TTFT 报告以客户端口径为准（应用内评测模式产出），本脚本输出的服
务端分段分布用于分段目标线对照与瓶颈定位。

用法：
  python compute_metrics.py --batch results.jsonl --export eval.jsonl
                            --qrels qrels.jsonl [--report report.json]
"""
import argparse
import json
import math
import sys
from pathlib import Path

# 分段目标线（毫秒，验收规范 §5.2）
SEGMENT_TARGETS_MS = {
    "retrieval_ms": 400,
    "rerank_ms": 800,
    "prompt_build_ms": 200,
    "model_ttft_ms": 3200,
}

# 无官方目标线的观察段：只报 P95 供瓶颈定位，不做达标判定
OBSERVED_SEGMENTS = ("resolve_ms",)

# 检索质量评定的候选排序依据：重排可用用重排名次，降级退 RRF 名次
def _candidate_rank(candidate: dict) -> int | None:
    return candidate.get("rerank_rank") or candidate.get("rrf_rank")


def _percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    return sorted(values)[min(round(ratio * (len(values) - 1)), len(values) - 1)]


def load_jsonl(path: str) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def ttft_report(batch_rows: list[dict]) -> dict:
    succeeded = [row for row in batch_rows if row.get("state") == "completed"]
    ttfts = [
        row["server_ttft_ms"] for row in succeeded if row.get("server_ttft_ms") is not None
    ]
    return {
        "total": len(batch_rows),
        "completed": len(succeeded),
        "failure_rate": round((len(batch_rows) - len(succeeded)) / len(batch_rows), 4)
        if batch_rows
        else None,
        "server_ttft_p50_ms": _percentile(ttfts, 0.50),
        "server_ttft_p95_ms": _percentile(ttfts, 0.95),
        "server_ttft_p99_ms": _percentile(ttfts, 0.99),
        "note": "客户端口径 TTFT（点击发送→首帧渲染）以应用内评测模式产出为准",
    }


def segment_report(export_rows: list[dict]) -> dict:
    """分段 P95 对照目标线（分段事实在查询运行表，经导出文件读取）"""
    succeeded = [row for row in export_rows if row.get("state") == "completed"]
    segments: dict[str, dict] = {}
    for field, target in SEGMENT_TARGETS_MS.items():
        values = [
            row[field] for row in succeeded if row.get(field) is not None
        ]
        p95 = _percentile(values, 0.95)
        segments[field] = {
            "target_ms": target,
            "p95_ms": p95,
            "meets_target": p95 is not None and p95 <= target,
        }
    for field in OBSERVED_SEGMENTS:
        values = [
            row[field] for row in succeeded if row.get(field) is not None
        ]
        segments[field] = {
            "target_ms": None,
            "p95_ms": _percentile(values, 0.95),
            "meets_target": None,
        }
    return segments


def load_qrels(path: str) -> dict[str, dict]:
    """分层 qrels：question 文本 → {relevant_docs, relevance}"""
    qrels: dict[str, dict] = {}
    for row in load_jsonl(path):
        question = (row.get("question") or "").strip()
        docs = row.get("relevant_docs")
        if not question or not docs:
            raise ValueError(f"qrels 行缺少 question 或 relevant_docs（必填层）: {row}")
        qrels[question] = {
            "relevant_docs": {docs[name] if isinstance(docs, dict) else name for name in docs},
            "relevance": row.get("relevance") or {},
        }
    return qrels


def retrieval_quality(export_rows: list[dict], qrels: dict[str, dict]) -> dict:
    """Recall@5 / MRR@10 / nDCG@10（按 question 文本对齐标注）"""
    evaluated = []
    recalls, reciprocal_ranks, ndcgs = [], [], []
    skipped = 0
    for row in export_rows:
        question = (row.get("question") or "").strip()
        if question not in qrels:
            skipped += 1
            continue
        if row.get("state") != "completed":
            skipped += 1
            continue
        relevant = qrels[question]["relevant_docs"]
        relevance = qrels[question]["relevance"]
        ranked = sorted(
            (c for c in row.get("candidates", []) if c.get("file_name")),
            key=_candidate_rank,
        )
        docs_in_order = []
        seen = set()
        for candidate in ranked[:10]:
            name = candidate["file_name"]
            if name not in seen:
                seen.add(name)
                docs_in_order.append(name)
        # Recall@5：前 5 名次内命中相关文档的比例
        top5 = set(docs_in_order[:5])
        recalls.append(len(top5 & relevant) / len(relevant) if relevant else 0.0)
        # MRR@10：首个相关命中排名倒数
        rr = 0.0
        for position, name in enumerate(docs_in_order, start=1):
            if name in relevant:
                rr = 1.0 / position
                break
        reciprocal_ranks.append(rr)
        # nDCG@10：分级缺失按 1（二元化），报告注明口径
        gains = [
            float(relevance.get(name, 1 if name in relevant else 0))
            for name in docs_in_order
        ]
        dcg = sum(gain / math.log2(position + 1) for position, gain in enumerate(gains, start=1))
        ideal_count = min(len(relevant), 10)
        ideal_gains = sorted(
            (float(relevance.get(name, 1)) for name in relevant), reverse=True
        )[:ideal_count]
        idcg = sum(gain / math.log2(position + 1) for position, gain in enumerate(ideal_gains, start=1))
        ndcgs.append(dcg / idcg if idcg > 0 else 0.0)
        evaluated.append(question)

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    return {
        "evaluated_questions": len(evaluated),
        "skipped_no_qrels_or_not_completed": skipped,
        "recall_at_5": mean(recalls),
        "mrr_at_10": mean(reciprocal_ranks),
        "ndcg_at_10": mean(ndcgs),
        "note": "nDCG 分级缺失时按二元相关性（命中=1）计算",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测指标计算（TTFT/分段/检索质量）")
    parser.add_argument("--batch", required=True, help="batch_queries.py 结果 JSONL")
    parser.add_argument("--export", required=True, help="export_evaluations.py 导出 JSONL")
    parser.add_argument("--qrels", required=True, help="分层标注 JSONL")
    parser.add_argument("--report", default=None, help="报告 JSON 输出路径")
    args = parser.parse_args(argv)

    try:
        qrels = load_qrels(args.qrels)
    except ValueError as exc:
        print(f"qrels 格式错误: {exc}", file=sys.stderr)
        return 2

    batch_rows = load_jsonl(args.batch)
    export_rows = load_jsonl(args.export)
    report = {
        "ttft": ttft_report(batch_rows),
        "segments": segment_report(export_rows),
        "retrieval_quality": retrieval_quality(export_rows, qrels),
    }
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
