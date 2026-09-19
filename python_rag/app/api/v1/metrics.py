# -*- coding: utf-8 -*-
"""API v1 指标路由：查询聚合指标（TTFT 分位、失败率、降级计数）

分位数采用最近邻秩法（写入注释保证口径可复现）；失败请求不进入
成功延迟分位（07 契约）。仅读取延迟值与状态计数，不加载正文。
"""
import sqlite3
from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .envelope import new_request_id, success_envelope

# 成功延迟分位的样本条件：completed 且非拒答（拒答无模型 TTFT）
_SUCCESS_SAMPLE_SQL = (
    "SELECT server_ttft_ms FROM query_runs"
    " WHERE state = 'completed' AND refused = 0 AND server_ttft_ms IS NOT NULL"
    "   AND (:kb_id IS NULL OR knowledge_base_id = :kb_id)"
    " ORDER BY server_ttft_ms"
)


@dataclass(frozen=True)
class MetricsDependencies:
    """指标端点依赖：由应用装配（或测试）构造"""

    run_repo: object  # QueryRunRepository（SQL 聚合经仓储连接执行）
    conn: sqlite3.Connection


def create_metrics_router(deps: MetricsDependencies) -> APIRouter:
    """装配指标路由（随 v1 路由挂载，路径前缀由父路由提供）"""
    router = APIRouter()

    @router.get("/metrics/queries")
    def query_metrics(knowledge_base_id: str | None = None) -> JSONResponse:
        """查询聚合指标：TTFT 分位、状态计数、失败率与降级计数"""
        request_id = new_request_id()
        params = {"kb_id": knowledge_base_id}
        ttft_values = [
            row[0]
            for row in deps.conn.execute(_SUCCESS_SAMPLE_SQL, params).fetchall()
        ]

        def _percentile(percent: float) -> int | None:
            """最近邻秩法：ceil(p * n) 位置的样本（1 起）"""
            if not ttft_values:
                return None
            import math

            rank = max(1, math.ceil(percent * len(ttft_values)))
            return ttft_values[rank - 1]

        state_rows = deps.conn.execute(
            "SELECT state, COUNT(*) FROM query_runs"
            " WHERE (:kb_id IS NULL OR knowledge_base_id = :kb_id)"
            " GROUP BY state",
            params,
        ).fetchall()
        counts = {row[0]: row[1] for row in state_rows}
        total = sum(counts.values())
        refused_row = deps.conn.execute(
            "SELECT COUNT(*) FROM query_runs"
            " WHERE refused = 1"
            "   AND (:kb_id IS NULL OR knowledge_base_id = :kb_id)",
            params,
        ).fetchone()
        degraded_row = deps.conn.execute(
            "SELECT COUNT(*) FROM query_runs"
            " WHERE rerank_degraded = 1"
            "   AND (:kb_id IS NULL OR knowledge_base_id = :kb_id)",
            params,
        ).fetchone()
        tokens_row = deps.conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)"
            " FROM query_runs"
            " WHERE (:kb_id IS NULL OR knowledge_base_id = :kb_id)",
            params,
        ).fetchone()
        failed = counts.get("failed", 0)
        payload = {
            "p50_ttft_ms": _percentile(0.50),
            "p95_ttft_ms": _percentile(0.95),
            "p99_ttft_ms": _percentile(0.99),
            "ttft_sample_size": len(ttft_values),
            "total": total,
            "completed": counts.get("completed", 0),
            "failed": failed,
            "cancelled": counts.get("cancelled", 0),
            "refused": refused_row[0],
            "degraded": degraded_row[0],
            "failure_rate": round(failed / total, 4) if total else None,
            "input_tokens": tokens_row[0],
            "output_tokens": tokens_row[1],
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    return router
