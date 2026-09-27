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
                            [--rank-by rerank,rrf,keyword] [--group-by chunking_config]
                            [--weak-spots-out weak_spots.jsonl]

排序依据（--rank-by）决定"按哪一路的名次评质量"：初筛取融合名次（rrf），
重排取重排名次（rerank，缺名次时退融合名次），亦可按单路名次（vector /
keyword）看该路自身质量。多个依据以逗号分隔时，首项为报告主体口径，其余
在 retrieval_quality_by_route 中并列输出，用于区分初筛与重排各自的贡献。
分组（--group-by）按导出字段分组重算质量，例如按 chunking_config 对比不同
切片参数；分组值缺失的运行单独计数，不并入任何组。
弱点题清单（--weak-spots-out）逐题标注排序靠后（首个相关命中不在第 1 位）、
召回不足（前 5 内无相关文档）与向量路零命中，供人工复核与后续构题。
切片级召回（--db）以引用快照为真值，回答"真正喂给生成的切片有没有被该路
召回"——被引父切片按其子切片是否被召回判定，故需读取事实库的父子关系。
路由消融（--ablation，需 --db）对比关键词路参与与不参与时的指标差值；不参与
一侧按候选的向量名次重排近似（候选集合固定为融合结果，属有界口径）。
"""
import argparse
import json
import math
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

# 分段目标线（毫秒）：超过即视为该段成为瓶颈
SEGMENT_TARGETS_MS = {
    "retrieval_ms": 400,
    "rerank_ms": 800,
    "prompt_build_ms": 200,
    "model_ttft_ms": 3200,
}

# 无官方目标线的观察段：只报 P95 供瓶颈定位，不做达标判定
OBSERVED_SEGMENTS = ("resolve_ms",)

# 缺失名次统一压到末位，避免排序键出现 None
_MISSING_RANK = 10**6

# 候选排序依据：同一批候选可分别按各路名次评估，用于区分初筛与重排的贡献。
# 重排口径保留"降级退 RRF 名次"的既有语义（重排失败时该路无名次）
_RANK_KEYS: dict[str, Callable[[dict], int]] = {
    "rerank": lambda c: c.get("rerank_rank") or c.get("rrf_rank") or _MISSING_RANK,
    "rrf": lambda c: c.get("rrf_rank") or _MISSING_RANK,
    "keyword": lambda c: c.get("keyword_rank") or _MISSING_RANK,
    "vector": lambda c: c.get("vector_rank") or _MISSING_RANK,
}

# 默认排序依据（与既有报告口径一致）
_DEFAULT_RANK_SOURCE = "rerank"


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


def _question_diagnostics(
    export_rows: list[dict], qrels: dict[str, dict], rank_key: Callable[[dict], int]
) -> list[dict]:
    """逐题事实：按指定名次排序后的文档序列与命中位次

    仅纳入有 qrels 标注且已完成的运行；供质量汇总与弱点题清单共用，
    两处口径同源避免同一批数据出现两种算法。
    """
    diagnostics: list[dict] = []
    for row in export_rows:
        question = (row.get("question") or "").strip()
        if question not in qrels or row.get("state") != "completed":
            continue
        relevant = qrels[question]["relevant_docs"]
        ranked = sorted(
            (c for c in row.get("candidates", []) if c.get("file_name")), key=rank_key
        )
        docs_in_order: list[str] = []
        seen = set()
        for candidate in ranked[:10]:
            name = candidate["file_name"]
            if name not in seen:
                seen.add(name)
                docs_in_order.append(name)
        first_rank = next(
            (position for position, name in enumerate(docs_in_order, start=1)
             if name in relevant),
            None,
        )
        candidates = row.get("candidates", [])
        diagnostics.append(
            {
                "question": question,
                "relevant_docs": sorted(relevant),
                "docs_in_order_top10": docs_in_order,
                "first_relevant_rank": first_rank,
                "recall_at_5": (
                    len(set(docs_in_order[:5]) & relevant) / len(relevant)
                    if relevant
                    else 0.0
                ),
                "relevance": qrels[question]["relevance"],
                "vector_hit_count": sum(
                    1 for c in candidates if c.get("vector_rank") is not None
                ),
                "keyword_hit_count": sum(
                    1 for c in candidates if c.get("keyword_rank") is not None
                ),
                "rerank_degraded": bool(row.get("rerank_degraded")),
                "refused": bool(row.get("refused")),
            }
        )
    return diagnostics


def retrieval_quality(
    export_rows: list[dict],
    qrels: dict[str, dict],
    rank_key: Callable[[dict], int] | None = None,
    *,
    rank_source: str = _DEFAULT_RANK_SOURCE,
) -> dict:
    """Recall@5 / MRR@10 / nDCG@10（按 question 文本对齐标注，按指定名次排序）"""
    key = rank_key or _RANK_KEYS[_DEFAULT_RANK_SOURCE]
    diagnostics = _question_diagnostics(export_rows, qrels, key)
    evaluated = [item["question"] for item in diagnostics]

    recalls = [item["recall_at_5"] for item in diagnostics]
    reciprocal_ranks = [
        1.0 / item["first_relevant_rank"] if item["first_relevant_rank"] else 0.0
        for item in diagnostics
    ]
    ndcgs = []
    for item in diagnostics:
        relevant = set(item["relevant_docs"])
        relevance = item["relevance"]
        gains = [
            float(relevance.get(name, 1 if name in relevant else 0))
            for name in item["docs_in_order_top10"]
        ]
        dcg = sum(
            gain / math.log2(position + 1)
            for position, gain in enumerate(gains, start=1)
        )
        ideal_gains = sorted(
            (float(relevance.get(name, 1)) for name in relevant), reverse=True
        )[: min(len(relevant), 10)]
        idcg = sum(
            gain / math.log2(position + 1)
            for position, gain in enumerate(ideal_gains, start=1)
        )
        ndcgs.append(dcg / idcg if idcg > 0 else 0.0)

    skipped = sum(
        1
        for row in export_rows
        if (row.get("question") or "").strip() not in qrels
        or row.get("state") != "completed"
    )
    return {
        "evaluated_questions": len(evaluated),
        "skipped_no_qrels_or_not_completed": skipped,
        "rank_source": rank_source,
        "recall_at_5": _mean(recalls),
        "mrr_at_10": _mean(reciprocal_ranks),
        "ndcg_at_10": _mean(ndcgs),
        "note": "nDCG 分级缺失时按二元相关性（命中=1）计算",
    }


def group_quality(
    export_rows: list[dict],
    qrels: dict[str, dict],
    group_field: str,
    rank_key: Callable[[dict], int] | None = None,
) -> dict:
    """按指定字段分组输出检索质量（例如按切片参数分组对比）

    分组值缺失的运行单独计数，不并入任何组——混入会让"参数对比"失真。
    """
    groups: dict[str, list[dict]] = {}
    skipped_no_group = 0
    for row in export_rows:
        value = row.get(group_field)
        if value is None:
            skipped_no_group += 1
            continue
        label = _group_label(value)
        groups.setdefault(label, []).append(row)
    return {
        "group_field": group_field,
        "skipped_no_group": skipped_no_group,
        "groups": [
            {
                "group": label,
                "rows": len(rows),
                **retrieval_quality(rows, qrels, rank_key),
            }
            for label, rows in sorted(groups.items())
        ],
    }


def _group_label(value: object) -> str:
    """分组标签：结构化配置压成可读短串，非字典值按其自身文本"""
    if not isinstance(value, dict):
        return str(value)
    keys = ("parent_chunk_chars", "child_chunk_chars")
    parts = [f"{key}={value[key]}" for key in keys if key in value]
    return ",".join(parts) if parts else json.dumps(value, ensure_ascii=False, sort_keys=True)


def weak_spots(
    export_rows: list[dict], qrels: dict[str, dict], rank_key: Callable[[dict], int]
) -> tuple[list[dict], dict]:
    """弱点题清单：排序靠后、召回不足与向量路零命中的题

    弱点分类：recall_miss（前 5 内无相关文档）、rank_low（首个相关命中不在
    第 1 位）、vector_zero（向量路无候选，属召回缺口信号）。清单按严重度
    排序，供人工复核与后续构题使用。
    """
    items: list[dict] = []
    for item in _question_diagnostics(export_rows, qrels, rank_key):
        weaknesses = []
        if item["recall_at_5"] < 1.0:
            weaknesses.append("recall_miss")
        if item["first_relevant_rank"] is None:
            weaknesses.append("no_relevant_in_top10")
        elif item["first_relevant_rank"] > 1:
            weaknesses.append("rank_low")
        if item["vector_hit_count"] == 0:
            weaknesses.append("vector_zero")
        if weaknesses:
            items.append({**item, "weaknesses": weaknesses})
    order = {"recall_miss": 0, "no_relevant_in_top10": 1, "rank_low": 2, "vector_zero": 3}
    items.sort(
        key=lambda item: (
            min(order[w] for w in item["weaknesses"]),
            -(item["first_relevant_rank"] or 99),
            item["question"],
        )
    )
    summary = {
        "total": len(items),
        "recall_miss": sum(1 for i in items if "recall_miss" in i["weaknesses"]),
        "no_relevant_in_top10": sum(
            1 for i in items if "no_relevant_in_top10" in i["weaknesses"]
        ),
        "rank_low": sum(1 for i in items if "rank_low" in i["weaknesses"]),
        "vector_zero": sum(1 for i in items if "vector_zero" in i["weaknesses"]),
    }
    return items, summary


def load_chunk_parents(db_path: str) -> tuple[dict[str, str | None], dict[str, set[str]]]:
    """读取切片父子关系：chunk_id -> 父 ID，以及 父 ID -> 子切片集合

    切片级度量需要它把候选（子切片）映射到被引切片（可能是父切片）：
    父子引用关系在事实库中维护，导出文件不携带该字段。
    """
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, parent_chunk_id FROM chunks").fetchall()
    finally:
        conn.close()
    parents = {row[0]: row[1] for row in rows}
    children: dict[str, set[str]] = {}
    for chunk_id, parent_id in parents.items():
        if parent_id:
            children.setdefault(parent_id, set()).add(chunk_id)
    return parents, children


def _cited_chunk_ids(row: dict) -> list[str]:
    """运行引用快照所指向的切片（被引切片即答案的事实载体）"""
    return [c["chunk_id"] for c in row.get("citations", []) if c.get("chunk_id")]


def _covered_by(cited: str, ranked: list[str], parents: dict[str, str | None],
                children: dict[str, set[str]]) -> bool:
    """被引切片是否被候选序列覆盖

    被引切片可能本身就是候选（子切片），也可能是父切片——父切片不在
    倒排与候选内，改判其任一子切片是否被召回。
    """
    if cited in ranked:
        return True
    return any(child in ranked for child in children.get(cited, ()))


def chunk_recall(
    export_rows: list[dict],
    parents: dict[str, str | None],
    children: dict[str, set[str]],
    rank_key: Callable[[dict], int],
    *,
    rank_source: str = _DEFAULT_RANK_SOURCE,
) -> dict:
    """切片级召回：各路名次下，答案所引切片是否落在候选前 k 内

    文档级指标只回答"相关文档有没有进前 5"，切片级回答"真正喂给生成的
    切片有没有被该路召回"——后者才决定生成阶段能拿到什么证据。真值取
    自引用快照（模型实际引用的切片），故其语义是"与最终引用一致"，不是
    绝对正确性；引用本身错误时该指标同步失真。
    """
    totals: dict[int, list[float]] = {5: [], 20: []}
    no_citation = 0
    for row in export_rows:
        cited = _cited_chunk_ids(row)
        if not cited:
            no_citation += 1
            continue
        ranked = [
            c["chunk_id"]
            for c in sorted(
                (c for c in row.get("candidates", []) if c.get("chunk_id")),
                key=rank_key,
            )
        ]
        for k, values in totals.items():
            top = ranked[:k]
            hit = sum(1 for item in cited if _covered_by(item, top, parents, children))
            values.append(hit / len(cited))
    return {
        "rank_source": rank_source,
        "evaluated_runs": len(totals[5]),
        "skipped_no_citation": no_citation,
        "chunk_recall_at_5": _mean(totals[5]),
        "chunk_recall_at_20": _mean(totals[20]),
        "note": "真值为引用快照所指向的切片；被引父切片按其子切片是否被召回判定",
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def route_ablation(
    export_rows: list[dict],
    qrels: dict[str, dict],
    parents: dict[str, str | None],
    children: dict[str, set[str]],
    rank_key: Callable[[dict], int],
) -> dict:
    """路由消融：关键词路参与与不参与时，各项指标的差值

    "不参与"用同一批候选按向量名次重排近似（候选集合已固定为融合结果，
    故这是有界口径：可测"关键词路是否改变排序与覆盖"，不可测"向量路
    自身漏了多少"）。差值即关键词路的增量贡献。
    """
    vector_key = _RANK_KEYS["vector"]
    full_quality = retrieval_quality(export_rows, qrels, rank_key)
    full_chunk = chunk_recall(export_rows, parents, children, rank_key)
    ablated_quality = retrieval_quality(export_rows, qrels, vector_key, rank_source="vector")
    ablated_chunk = chunk_recall(export_rows, parents, children, vector_key)

    def delta(with_keyword: object, without: object) -> float | None:
        if not isinstance(with_keyword, float) or not isinstance(without, float):
            return None
        return round(with_keyword - without, 4)

    return {
        "with_keyword": {
            "retrieval_quality": full_quality,
            "chunk_recall": full_chunk,
        },
        "without_keyword": {
            "retrieval_quality": ablated_quality,
            "chunk_recall": ablated_chunk,
        },
        "delta": {
            "recall_at_5": delta(full_quality["recall_at_5"], ablated_quality["recall_at_5"]),
            "mrr_at_10": delta(full_quality["mrr_at_10"], ablated_quality["mrr_at_10"]),
            "ndcg_at_10": delta(full_quality["ndcg_at_10"], ablated_quality["ndcg_at_10"]),
            "chunk_recall_at_5": delta(
                full_chunk["chunk_recall_at_5"], ablated_chunk["chunk_recall_at_5"]
            ),
            "chunk_recall_at_20": delta(
                full_chunk["chunk_recall_at_20"], ablated_chunk["chunk_recall_at_20"]
            ),
        },
        "note": "无关键词口径按候选的向量名次重排近似（候选集合固定为融合结果）",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测指标计算（TTFT/分段/检索质量）")
    parser.add_argument("--batch", required=True, help="batch_queries.py 结果 JSONL")
    parser.add_argument("--export", required=True, help="export_evaluations.py 导出 JSONL")
    parser.add_argument("--qrels", required=True, help="分层标注 JSONL")
    parser.add_argument("--report", default=None, help="报告 JSON 输出路径")
    parser.add_argument(
        "--rank-by",
        default=_DEFAULT_RANK_SOURCE,
        help=(
            "候选排序依据，逗号分隔可多选："
            + "/".join(_RANK_KEYS)
            + "（首个为报告主体口径，多选时额外输出各路对照）"
        ),
    )
    parser.add_argument(
        "--group-by",
        default=None,
        help="按导出字段分组输出检索质量（如 chunking_config，用于切片参数对比）",
    )
    parser.add_argument(
        "--weak-spots-out",
        default=None,
        help="弱点题清单输出路径（JSONL：排序靠后/召回不足/向量路零命中）",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="事实库路径：给出后输出切片级召回（需切片父子关系，导出文件不携带）",
    )
    parser.add_argument(
        "--ablation",
        action="store_true",
        help="输出路由消融：关键词路参与与不参与时的指标差值（需 --db）",
    )
    args = parser.parse_args(argv)

    if args.ablation and not args.db:
        print("--ablation 需同时给出 --db（消融需要切片父子关系）", file=sys.stderr)
        return 2

    sources = [item.strip() for item in args.rank_by.split(",") if item.strip()]
    unknown = [item for item in sources if item not in _RANK_KEYS]
    if not sources or unknown:
        print(f"非法排序依据: {unknown or args.rank_by}", file=sys.stderr)
        return 2

    try:
        qrels = load_qrels(args.qrels)
    except ValueError as exc:
        print(f"qrels 格式错误: {exc}", file=sys.stderr)
        return 2

    batch_rows = load_jsonl(args.batch)
    export_rows = load_jsonl(args.export)
    primary = sources[0]
    report = {
        "ttft": ttft_report(batch_rows),
        "segments": segment_report(export_rows),
        # 主体口径：默认 rerank，与既有报告逐位可比
        "retrieval_quality": retrieval_quality(
            export_rows, qrels, _RANK_KEYS[primary], rank_source=primary
        ),
        # 各路对照：区分初筛（rrf）与重排（rerank）的贡献
        "retrieval_quality_by_route": {
            source: retrieval_quality(
                export_rows, qrels, _RANK_KEYS[source], rank_source=source
            )
            for source in sources
        },
    }
    if args.group_by:
        report["retrieval_quality_by_group"] = group_quality(
            export_rows, qrels, args.group_by, _RANK_KEYS[primary]
        )
    if args.db:
        parents, children = load_chunk_parents(args.db)
        report["chunk_recall_by_route"] = {
            source: chunk_recall(
                export_rows, parents, children, _RANK_KEYS[source], rank_source=source
            )
            for source in sources
        }
        if args.ablation:
            report["route_ablation"] = route_ablation(
                export_rows, qrels, parents, children, _RANK_KEYS[primary]
            )
    items, summary = weak_spots(export_rows, qrels, _RANK_KEYS[primary])
    report["weak_spot_summary"] = summary
    if args.weak_spots_out:
        Path(args.weak_spots_out).write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"弱点题清单已写入: {args.weak_spots_out}（{len(items)} 题）")
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
