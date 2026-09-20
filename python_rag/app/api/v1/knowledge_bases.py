# -*- coding: utf-8 -*-
"""API v1 知识库路由：创建、列表、详情、重命名、删除与健康检查任务

端点以闭包持有依赖（运行时由应用装配注入，测试可替换），响应统一
走 v1 信封。删除为队列长操作：容量检查、软删除与删除任务创建同一
事务（202 返回任务），队列满时整体回滚、知识库保持未删除。
"""
from dataclasses import dataclass

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.domain.errors import (
    DuplicateActiveNameError,
    EntityNotFoundError,
    TaskQueueFullError,
    TaskStateConflictError,
)
from app.domain.ports import DeletionRepository, KnowledgeBaseRepository, TaskRepository

from .envelope import error_envelope, new_request_id, serialize_task, success_envelope
from .pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor


@dataclass(frozen=True)
class KnowledgeBaseDependencies:
    """知识库端点依赖：由应用装配（或测试）构造"""

    kb_repo: KnowledgeBaseRepository
    deletion_repo: DeletionRepository
    task_repo: TaskRepository


class KnowledgeBaseCreateBody(BaseModel):
    """创建知识库请求体"""

    name: str
    description: str | None = None


class KnowledgeBasePatchBody(BaseModel):
    """重命名知识库请求体（description 传当前值即保持不变）"""

    name: str
    description: str | None = None


def serialize_kb(kb) -> dict:
    """知识库实体到响应视图（软删除时间随实体透出）"""
    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "status": kb.status.value,
        "deleted_at": kb.deleted_at,
        "delete_requested_at": kb.delete_requested_at,
        "created_at": kb.created_at,
        "updated_at": kb.updated_at,
    }


def create_knowledge_bases_router(deps: KnowledgeBaseDependencies) -> APIRouter:
    """装配知识库路由（随 v1 路由挂载，路径前缀由父路由提供）"""
    router = APIRouter()

    @router.post("/knowledge-bases")
    def create_knowledge_base(body: KnowledgeBaseCreateBody) -> JSONResponse:
        """创建知识库（201）；活动名称唯一，冲突按 409 表达"""
        request_id = new_request_id()
        if not body.name.strip():
            return JSONResponse(
                status_code=400,
                content=error_envelope(request_id, "INVALID_PARAM", "名称不能为空"),
            )
        try:
            kb = deps.kb_repo.create(body.name.strip(), body.description)
        except DuplicateActiveNameError as exc:
            return JSONResponse(
                status_code=409,
                content=error_envelope(request_id, "NAME_CONFLICT", str(exc)),
            )
        return JSONResponse(
            status_code=201,
            content=success_envelope(serialize_kb(kb), request_id),
        )

    @router.get("/knowledge-bases")
    def list_knowledge_bases(
        request: Request, cursor: str | None = None, limit: int | None = None
    ) -> JSONResponse:
        """活动知识库列表（keyset 分页，按创建时间倒序）"""
        request_id = new_request_id()
        try:
            page_size = clamp_limit(limit)
            after_created_at, after_id = (
                decode_cursor(cursor) if cursor else (None, None)
            )
        except InvalidCursorError as exc:
            return JSONResponse(
                status_code=400,
                content=error_envelope(request_id, "INVALID_PARAM", str(exc)),
            )
        items = deps.kb_repo.list_active(
            limit=page_size,
            after_created_at=after_created_at,
            after_id=after_id,
        )
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id)
            if len(items) == page_size
            else None
        )
        payload = {
            "items": [serialize_kb(kb) for kb in items],
            "next_cursor": next_cursor,
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/knowledge-bases/{kb_id}")
    def get_knowledge_base(kb_id: str) -> JSONResponse:
        """知识库详情（不存在或已删除按 404 表达）"""
        request_id = new_request_id()
        kb = deps.kb_repo.get(kb_id)
        if kb is None:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "KNOWLEDGE_BASE_NOT_FOUND", "知识库不存在"
                ),
            )
        return JSONResponse(
            status_code=200,
            content=success_envelope(serialize_kb(kb), request_id),
        )

    @router.patch("/knowledge-bases/{kb_id}")
    def rename_knowledge_base(
        kb_id: str, body: KnowledgeBasePatchBody
    ) -> JSONResponse:
        """重命名知识库并可更新描述"""
        request_id = new_request_id()
        if not body.name.strip():
            return JSONResponse(
                status_code=400,
                content=error_envelope(request_id, "INVALID_PARAM", "名称不能为空"),
            )
        try:
            kb = deps.kb_repo.rename(kb_id, body.name.strip(), body.description)
        except EntityNotFoundError:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "KNOWLEDGE_BASE_NOT_FOUND", "知识库不存在或已删除"
                ),
            )
        except DuplicateActiveNameError as exc:
            return JSONResponse(
                status_code=409,
                content=error_envelope(request_id, "NAME_CONFLICT", str(exc)),
            )
        return JSONResponse(
            status_code=200,
            content=success_envelope(serialize_kb(kb), request_id),
        )

    @router.delete("/knowledge-bases/{kb_id}")
    def delete_knowledge_base(
        kb_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        """删除知识库（软删除 + 物理清理任务，同一事务）：202 返回任务"""
        request_id = new_request_id()
        try:
            task = deps.deletion_repo.create_kb_deletion(
                kb_id, idempotency_key=idempotency_key
            )
        except EntityNotFoundError:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "KNOWLEDGE_BASE_NOT_FOUND", "知识库不存在"
                ),
            )
        except TaskStateConflictError as exc:
            return JSONResponse(
                status_code=409,
                content=error_envelope(
                    request_id, "TASK_STATE_CONFLICT", str(exc)
                ),
            )
        except TaskQueueFullError as exc:
            return JSONResponse(
                status_code=429,
                content=error_envelope(request_id, "TASK_QUEUE_FULL", str(exc)),
            )
        return JSONResponse(
            status_code=202,
            content=success_envelope({"task": serialize_task(task)}, request_id),
        )

    @router.post("/health/knowledge-bases/{kb_id}")
    def create_kb_health_check(
        kb_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        """创建知识库健康检查任务（比对活动索引与切片事实）"""
        request_id = new_request_id()
        kb = deps.kb_repo.get(kb_id)
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
            task = deps.task_repo.create(
                "health_check",
                knowledge_base_id=kb_id,
                idempotency_key=idempotency_key,
            )
        except TaskQueueFullError as exc:
            return JSONResponse(
                status_code=429,
                content=error_envelope(request_id, "TASK_QUEUE_FULL", str(exc)),
            )
        return JSONResponse(
            status_code=202,
            content=success_envelope({"task": serialize_task(task)}, request_id),
        )

    return router
