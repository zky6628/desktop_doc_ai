# -*- coding: utf-8 -*-
"""API v1 会话路由：会话列表、历史消息与删除

端点以闭包持有依赖（运行时由应用装配注入，测试可替换），响应统一
走 v1 信封。已删除知识库的历史会话仍可读（410 只约束新建会话与
查询）；删除为立即生效的非队列操作，消息与引用随存储层级联清除，
查询指标的会话关联由外键置空（指标事实保留）。
"""
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.domain.errors import EntityNotFoundError
from app.domain.ports import ConversationRepository, KnowledgeBaseRepository

from .envelope import error_envelope, new_request_id, success_envelope
from .pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor


@dataclass(frozen=True)
class ConversationDependencies:
    """会话端点依赖：由应用装配（或测试）构造"""

    kb_repo: KnowledgeBaseRepository
    conversation_repo: ConversationRepository


def create_conversations_router(deps: ConversationDependencies) -> APIRouter:
    """装配会话路由（随 v1 路由挂载，路径前缀由父路由提供）"""
    router = APIRouter()

    @router.get("/conversations")
    def list_conversations(
        request: Request,
        kb_id: str,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> JSONResponse:
        """知识库会话列表（keyset 分页，最近活跃倒序，含最后消息摘要）"""
        request_id = new_request_id()
        kb = deps.kb_repo.get(kb_id)
        if kb is None:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "KNOWLEDGE_BASE_NOT_FOUND", "知识库不存在"
                ),
            )
        try:
            page_size = clamp_limit(limit)
            after_updated_at, after_id = (
                decode_cursor(cursor) if cursor else (None, None)
            )
        except InvalidCursorError as exc:
            return JSONResponse(
                status_code=400,
                content=error_envelope(request_id, "INVALID_PARAM", str(exc)),
            )
        items = deps.conversation_repo.list_by_knowledge_base(
            kb_id,
            limit=page_size,
            after_updated_at=after_updated_at,
            after_id=after_id,
        )
        next_cursor = (
            encode_cursor(items[-1].updated_at, items[-1].id)
            if len(items) == page_size
            else None
        )
        payload = {
            "items": [_summary_view(item) for item in items],
            "next_cursor": next_cursor,
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/conversations/{conversation_id}/messages")
    def list_messages(conversation_id: str) -> JSONResponse:
        """历史消息（升序全量；已删除知识库的会话仍可读）"""
        request_id = new_request_id()
        try:
            messages = deps.conversation_repo.list_messages(conversation_id)
        except EntityNotFoundError:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "CONVERSATION_NOT_FOUND", "会话不存在"
                ),
            )
        payload = {
            "conversation_id": conversation_id,
            "items": [_message_view(message) for message in messages],
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.delete("/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str) -> Response:
        """删除会话（立即生效；查询指标事实保留）"""
        request_id = new_request_id()
        try:
            deps.conversation_repo.delete(conversation_id)
        except EntityNotFoundError:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "CONVERSATION_NOT_FOUND", "会话不存在"
                ),
            )
        return Response(status_code=204)

    return router


def _summary_view(summary) -> dict:
    """会话摘要的响应视图（无消息时 last_message 为 null）"""
    return {
        "id": summary.id,
        "knowledge_base_id": summary.knowledge_base_id,
        "title": summary.title,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "last_message": (
            None
            if summary.last_message_role is None
            else {
                "role": summary.last_message_role,
                "content_excerpt": summary.last_message_excerpt,
                "created_at": summary.last_message_created_at,
            }
        ),
    }


def _message_view(message) -> dict:
    """消息的响应视图"""
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at,
    }
