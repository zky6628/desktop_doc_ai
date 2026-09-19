# -*- coding: utf-8 -*-
"""API v1 查询路由：创建查询、断线恢复、SSE 流与取消

端点以闭包持有查询依赖（运行时由应用装配注入，测试可替换），响应
统一走 v1 信封。执行与消费解耦：POST 只创建运行并启动后台线程即返
回 202；SSE 端点轮询事件存储（Last-Event-ID 断点重放、10 秒心跳、
终态关流）；断线不取消生成，取消走显式端点在检查点生效。
"""
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.domain.errors import EntityNotFoundError
from app.domain.ports import (
    CitationRepository,
    ConversationRepository,
    KnowledgeBaseRepository,
    QueryRunRepository,
)

from .envelope import error_envelope, new_request_id, success_envelope

if TYPE_CHECKING:
    from app.infrastructure.query import QueryOrchestrator

# SSE 轮询与心跳节奏
_POLL_INTERVAL_SECONDS = 0.05
_HEARTBEAT_INTERVAL_SECONDS = 10.0

# 终态事件类型：到达即关闭 SSE 流
_TERMINAL_EVENT_TYPES = {"done", "error", "cancelled"}


@dataclass(frozen=True)
class QueryDependencies:
    """查询端点依赖：由应用装配（或测试）构造"""

    orchestrator: "QueryOrchestrator"
    run_repo: QueryRunRepository
    kb_repo: KnowledgeBaseRepository
    conversation_repo: ConversationRepository
    citation_repo: CitationRepository


class QueryCreateBody(BaseModel):
    """查询创建请求体"""

    knowledge_base_id: str
    question: str
    conversation_id: str | None = None


def create_query_router(deps: QueryDependencies) -> APIRouter:
    """装配查询路由（随 v1 路由挂载，路径前缀由父路由提供）

    :param deps: 查询端点依赖
    :return: 查询端点 APIRouter（/queries、/queries/{id} 等）
    """
    router = APIRouter()

    @router.post("/queries")
    def create_query(
        body: QueryCreateBody,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        """创建查询运行并启动后台执行（202 + 流地址）"""
        request_id = new_request_id()
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
        try:
            run = deps.orchestrator.start_query(
                kb_id=body.knowledge_base_id,
                question=body.question,
                conversation_id=body.conversation_id,
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content=error_envelope(request_id, "INVALID_PARAM", str(exc)),
            )
        payload = {
            "query_id": run.id,
            "stream_url": f"/api/v1/queries/{run.id}/events",
            "state": run.state.value,
        }
        return JSONResponse(
            status_code=202, content=success_envelope(payload, request_id)
        )

    @router.get("/queries/{query_id}")
    def get_query(query_id: str) -> JSONResponse:
        """断线恢复/最终结果：聚合正文、引用与状态"""
        request_id = new_request_id()
        run = deps.orchestrator.get_run(query_id)
        if run is None:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "QUERY_NOT_FOUND", "查询不存在"
                ),
            )
        answer = (
            deps.conversation_repo.get_message_content(run.assistant_message_id)
            if run.assistant_message_id
            else None
        )
        citations = (
            deps.citation_repo.list_by_message(run.assistant_message_id)
            if run.assistant_message_id
            else []
        )
        payload = {
            "query_id": run.id,
            "state": run.state.value,
            "refused": run.refused,
            "rerank_degraded": run.rerank_degraded,
            "server_ttft_ms": run.server_ttft_ms,
            "total_ms": run.total_ms,
            "error": (
                None
                if run.error_code is None
                else {"code": run.error_code, "message": run.error_message}
            ),
            "answer": answer,
            "citations": [_citation_view(record) for record in citations],
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/queries/{query_id}/events")
    def stream_events(
        query_id: str,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        """SSE 事件流：断点重放、心跳与终态关流"""
        request_id = new_request_id()
        run = deps.orchestrator.get_run(query_id)
        if run is None:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "QUERY_NOT_FOUND", "查询不存在"
                ),
            )
        after_seq = _parse_last_event_id(last_event_id)
        return StreamingResponse(
            _event_stream(query_id, after_seq),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/queries/{query_id}/cancel")
    def cancel_query(query_id: str) -> JSONResponse:
        """请求取消查询（幂等；检查点生效，保留已生成正文与引用）"""
        request_id = new_request_id()
        try:
            run = deps.orchestrator.request_cancel(query_id)
        except EntityNotFoundError:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "QUERY_NOT_FOUND", "查询不存在"
                ),
            )
        return JSONResponse(
            status_code=200,
            content=success_envelope(
                {"query_id": run.id, "state": run.state.value}, request_id
            ),
        )

    def _event_stream(query_id: str, after_seq: int):
        """SSE 事件生成器：轮询事件存储，重放按 Last-Event-ID 续传"""
        last_heartbeat = time.monotonic()
        cursor = after_seq
        while True:
            events = deps.orchestrator.read_events(query_id, cursor)
            for event in events:
                yield _format_sse(event)
                cursor = event.event_seq
                last_heartbeat = time.monotonic()
                if event.event_type in _TERMINAL_EVENT_TYPES:
                    return
            elapsed = time.monotonic() - last_heartbeat
            if elapsed >= _HEARTBEAT_INTERVAL_SECONDS:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
            time.sleep(_POLL_INTERVAL_SECONDS)

    return router


def _parse_last_event_id(raw: str | None) -> int:
    """解析 Last-Event-ID 头（非法值按从零重放处理）"""
    if not raw:
        return 0
    try:
        return max(int(raw), 0)
    except ValueError:
        return 0


def _format_sse(event) -> str:
    """把事件格式化为 SSE 帧（id/event/data）"""
    if event.event_type == "tokens":
        data = json.dumps(
            {
                "from": event.token_seq_start,
                "to": event.token_seq_end,
                "text": event.token_text,
            },
            ensure_ascii=False,
        )
    else:
        data = event.payload_json or "{}"
    return f"id: {event.event_seq}\nevent: {event.event_type}\ndata: {data}\n\n"


def _citation_view(record) -> dict:
    """引用快照的响应视图（对齐 CitationDTO 子集）"""
    return {
        "id": record.citation_id,
        "chunk_id": record.chunk_id,
        "document_id": record.document_id_snapshot,
        "document_version_id": record.document_version_id_snapshot,
        "file_name": record.file_name_snapshot,
        "version_no": record.version_no_snapshot,
        "page_no": record.page_no,
        "section_path": record.section_path,
        "content": record.content_snapshot,
        "validation_state": record.validation_state,
        "vector_score": record.vector_score,
        "keyword_score": record.keyword_score,
        "fusion_score": record.fusion_score,
        "rerank_score": record.rerank_score,
    }
