# -*- coding: utf-8 -*-
"""API v1 运维路由：聚合健康探针与无密钥运行配置概览

健康检查为轻量只读探针：不外呼云服务（健康检查不产生调用成本与
秒级延迟），组件降级以数据表达而非错误状态；仅数据库不可达时整条
API 已不可用，按 503 错误信封返回。配置概览只输出无密钥的运行事实
（模型身份、在役配置摘要、容量常量与功能开关）；配置摘要按类型以
固定白名单键输出，prompt/generation 模板等大文本与后续扩展字段不
外泄。密钥状态只表达 configured/unconfigured，不验证有效性。
"""
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.domain import file_policy, task_state
from app.domain.embedding import EMBEDDING_MODEL
from app.domain.generation import GENERATION_MODEL
from app.domain.rerank import RERANK_MODEL

from .envelope import error_envelope, new_request_id, success_envelope


class HeartbeatClient(Protocol):
    """向量库客户端探活协议：健康探针仅依赖心跳方法"""

    def heartbeat(self) -> object: ...

# 在役配置行的摘要白名单：按 config_type 固定键集输出
# （键缺失自动省略，类型不在表内则整行跳过）
_CONFIG_SUMMARY_KEYS = {
    "retrieval": frozenset(
        {"vector_top_k", "keyword_top_k", "fused_top_k", "rrf_k"}
    ),
    "chunking": frozenset({"parent_chunk_chars", "child_chunk_chars"}),
    "embedding": frozenset({"model", "dimensions"}),
    "rerank": frozenset({"model", "top_n"}),
    "generation": frozenset({"model", "max_output_tokens"}),
}

# 当前生效的模型身份（网关装配与域常量同源；模型档案表为后续
# 多 Profile 预留，当前无写入方，不输出空事实）
_MODEL_PROFILES = [
    {"role": "embedding", "provider": "dashscope", "model_name": EMBEDDING_MODEL},
    {"role": "rerank", "provider": "dashscope", "model_name": RERANK_MODEL},
    {"role": "generation", "provider": "dashscope", "model_name": GENERATION_MODEL},
]


@dataclass(frozen=True)
class OpsDependencies:
    """运维端点依赖：由应用装配（或测试）构造

    worker_alive 为工作线程存活谓词（Worker 未启用时调用方不给探针
    机会，直接按 stopped 表达）；凭据配置状态由装配期环境判定
    """

    conn: sqlite3.Connection
    chroma_client: HeartbeatClient  # Chroma 客户端或测试替身
    worker_enabled: bool
    worker_alive: Callable[[], bool]
    mineru_configured: bool
    dashscope_configured: bool
    local_debug_enabled: bool


def create_ops_router(deps: OpsDependencies) -> APIRouter:
    """装配运维路由（随 v1 路由挂载，路径前缀由父路由提供）"""
    router = APIRouter()

    @router.get("/health")
    def health() -> JSONResponse:
        """聚合健康探针：核心组件状态、队列水位与凭据配置状态"""
        request_id = new_request_id()
        try:
            queue_row = deps.conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN state = 'running' THEN 1 ELSE 0 END), 0),"
                " COALESCE(SUM(CASE WHEN state = 'queued' THEN 1 ELSE 0 END), 0)"
                " FROM tasks"
            ).fetchone()
        except sqlite3.Error:
            # 数据库不可达时整条 API 已不可用，健康检查自身按错误表达
            return JSONResponse(
                status_code=503,
                content=error_envelope(
                    request_id,
                    "INTERNAL_ERROR",
                    "数据库不可用",
                    retryable=True,
                ),
            )
        sqlite_state = "ok"
        chroma_state = "ok"
        try:
            deps.chroma_client.heartbeat()
        except Exception:  # noqa: BLE001 - 探针失败即降级，不向调用方传播
            chroma_state = "error"
        fts_state = "ok"
        try:
            deps.conn.execute("SELECT 1 FROM chunks_fts LIMIT 1")
        except sqlite3.Error:
            fts_state = "error"
        if deps.worker_enabled:
            worker_state = "ok" if deps.worker_alive() else "error"
        else:
            worker_state = "stopped"

        components = {
            "sqlite": {"state": sqlite_state},
            "chroma": {"state": chroma_state, "detail": None},
            "fts": {"state": fts_state},
            "worker": {
                "state": worker_state,
                "worker_enabled": deps.worker_enabled,
            },
            "providers": {
                "mineru": {"configured": deps.mineru_configured},
                "dashscope": {"configured": deps.dashscope_configured},
            },
        }
        # 凭据未配置不影响整体状态（本地路线完整可用是合法运行形态）；
        # 降级只看核心链路组件
        degraded_components = [
            name
            for name, state in (
                ("chroma", chroma_state),
                ("fts", fts_state),
                ("worker", worker_state),
            )
            if state != "ok"
        ]
        payload = {
            "status": "degraded" if degraded_components else "healthy",
            "components": components,
            "queue": {
                "running": queue_row[0],
                "pending": queue_row[1],
                "capacity_running": task_state.MAX_RUNNING,
                "capacity_pending": task_state.MAX_PENDING,
                "capacity_non_terminal": task_state.MAX_NON_TERMINAL,
            },
            "degraded": degraded_components,
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/config/public")
    def public_config() -> JSONResponse:
        """无密钥运行配置概览：模型身份、在役配置摘要、容量与开关"""
        request_id = new_request_id()
        rows = deps.conn.execute(
            "SELECT config_type, version, config_json FROM pipeline_configs"
            " WHERE retired_at IS NULL ORDER BY config_type, version"
        ).fetchall()
        summaries = []
        for config_type, version, config_json in rows:
            allowed = _CONFIG_SUMMARY_KEYS.get(config_type)
            if allowed is None:
                continue
            content = json.loads(config_json)
            summaries.append(
                {
                    "config_type": config_type,
                    "version": version,
                    "config_summary": {
                        key: content[key] for key in allowed if key in content
                    },
                }
            )
        payload = {
            "model_profiles": _MODEL_PROFILES,
            "pipeline_configs": summaries,
            "limits": {
                "max_running": task_state.MAX_RUNNING,
                "max_pending": task_state.MAX_PENDING,
                "max_non_terminal": task_state.MAX_NON_TERMINAL,
                "max_file_mb": file_policy.MAX_FILE_BYTES // (1024 * 1024),
                "max_batch_files": file_policy.MAX_BATCH_FILES,
            },
            "features": {
                "worker_enabled": deps.worker_enabled,
                "local_debug_enabled": deps.local_debug_enabled,
                "cloud_parsing_available": deps.mineru_configured,
            },
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    return router
