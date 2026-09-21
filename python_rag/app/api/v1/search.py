# -*- coding: utf-8 -*-
"""API v1 调试检索路由：本地调试模式的检索链路观察端点

仅在显式开启本地调试时可用（发布包默认关闭，未开启时端点整体拒绝
——调试攻击面最小化）；复用查询链路的检索与重排实现（含瞬态降级
语义），不创建查询运行、不落候选快照与指标——调试观察零写入。
Top-K 覆盖仅本端点接受且范围 1..50；候选只含定位事实与各阶段分数，
正文经既有文档块端点获取，不在调试响应中透出。
"""
from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.domain.errors import RerankError
from app.domain.ports import KnowledgeBaseRepository, RerankGateway
from app.infrastructure.retrieval import (
    ContextResolver,
    RetrievalService,
    rank_with_rerank,
)

from .envelope import error_envelope, new_request_id, success_envelope

# 覆盖参数合法域：召回/融合数量与重排入选数的统一上限
_MAX_OVERRIDE_TOP_K = 50


@dataclass(frozen=True)
class SearchDependencies:
    """调试检索端点依赖：由应用装配（或测试）构造

    检索服务、事实解析器与重排网关与查询链路共享同一实例，保证
    调试观察到的行为与生产一致
    """

    kb_repo: KnowledgeBaseRepository
    retrieval_service: RetrievalService
    resolver: ContextResolver
    rerank_gateway: RerankGateway
    local_debug_enabled: bool


class SearchBody(BaseModel):
    """调试检索请求体：覆盖参数为空表示沿用服务端配置值"""

    knowledge_base_id: str
    question: str
    vector_top_k: int | None = None
    keyword_top_k: int | None = None
    fused_top_k: int | None = None
    rerank_top_n: int | None = None


def create_search_router(deps: SearchDependencies) -> APIRouter:
    """装配调试检索路由（随 v1 路由挂载，路径前缀由父路由提供）"""
    router = APIRouter()

    @router.post("/search")
    def search(body: SearchBody) -> JSONResponse:
        """本地调试检索：各阶段候选明细与阶段规模（零写入）"""
        request_id = new_request_id()
        if not deps.local_debug_enabled:
            return JSONResponse(
                status_code=422,
                content=error_envelope(
                    request_id,
                    "DEBUG_OVERRIDE_NOT_ALLOWED",
                    "本地调试检索未开启",
                ),
            )
        kb = deps.kb_repo.get(body.knowledge_base_id)
        if kb is None:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "KNOWLEDGE_BASE_NOT_FOUND", "知识库不存在"
                ),
            )
        if kb.deleted_at is not None:
            return JSONResponse(
                status_code=410,
                content=error_envelope(
                    request_id, "KNOWLEDGE_BASE_DELETED", "知识库已删除"
                ),
            )
        overrides = {
            "vector_top_k": body.vector_top_k,
            "keyword_top_k": body.keyword_top_k,
            "fused_top_k": body.fused_top_k,
            "rerank_top_n": body.rerank_top_n,
        }
        invalid = [
            name
            for name, value in overrides.items()
            if value is not None and not 1 <= value <= _MAX_OVERRIDE_TOP_K
        ]
        if invalid:
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    request_id,
                    "INVALID_PARAM",
                    f"覆盖参数取值须在 1..{_MAX_OVERRIDE_TOP_K}: {', '.join(invalid)}",
                ),
            )
        try:
            outcome = deps.retrieval_service.retrieve(
                body.knowledge_base_id,
                body.question,
                vector_top_k=body.vector_top_k,
                keyword_top_k=body.keyword_top_k,
                fused_top_k=body.fused_top_k,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content=error_envelope(request_id, "INVALID_PARAM", str(exc)),
            )

        candidates_payload = []
        if outcome.candidates:
            sources = {
                source.chunk_id: source
                for source in deps.resolver.resolve(body.knowledge_base_id)
            }
            candidate_texts = [
                sources[candidate.chunk_id].content
                for candidate in outcome.candidates
                if candidate.chunk_id in sources
            ]
            try:
                ranking = rank_with_rerank(
                    deps.rerank_gateway,
                    body.question,
                    outcome.candidates,
                    candidate_texts,
                    rerank_top_n=body.rerank_top_n,
                )
            except RerankError as exc:
                # 非瞬态重排失败（认证/配额/协议）不降级也不吞错：调试
                # 观察必须暴露真实失败，错误族沿用查询链路的失败码
                return JSONResponse(
                    status_code=502,
                    content=error_envelope(
                        request_id, exc.error_code, str(exc)
                    ),
                )
            rerank_rank_by_chunk = {
                chunk_id: position
                for position, chunk_id in enumerate(ranking.ranked_ids, start=1)
            }
            for candidate in outcome.candidates:
                source = sources.get(candidate.chunk_id)
                candidates_payload.append(
                    {
                        "chunk_id": candidate.chunk_id,
                        "document_id": (
                            source.document_id if source is not None else None
                        ),
                        "document_version_id": candidate.document_version_id,
                        "file_name": (
                            source.file_name if source is not None else None
                        ),
                        "version_no": (
                            source.version_no if source is not None else None
                        ),
                        "page_no": (
                            source.page_start if source is not None else None
                        ),
                        "section_path": (
                            source.section_path if source is not None else None
                        ),
                        "vector_rank": candidate.vector_rank,
                        "vector_score": candidate.vector_score,
                        "keyword_rank": candidate.keyword_rank,
                        "keyword_score": candidate.keyword_score,
                        "rrf_rank": candidate.rrf_rank,
                        "rrf_score": candidate.rrf_score,
                        "rerank_rank": (
                            rerank_rank_by_chunk.get(candidate.chunk_id)
                            if not ranking.degraded
                            else None
                        ),
                        "rerank_score": ranking.rerank_scores.get(
                            candidate.chunk_id
                        ),
                    }
                )
            reranked_count = len(ranking.ranked_ids)
            rerank_degraded = ranking.degraded
        else:
            # 无候选不触达重排（与查询链路的拒答口径一致）
            reranked_count = 0
            rerank_degraded = False

        payload = {
            "candidates": candidates_payload,
            "stages": {
                "vector_hits": outcome.vector_hit_count,
                "keyword_hits": outcome.keyword_hit_count,
                "fused": len(outcome.candidates),
                "reranked": reranked_count,
                "rerank_degraded": rerank_degraded,
                "dropped_hit_count": outcome.dropped_hit_count,
            },
            "config": {
                "overridden": {
                    name: value for name, value in overrides.items() if value is not None
                }
            },
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    return router
