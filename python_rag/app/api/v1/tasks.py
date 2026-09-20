# -*- coding: utf-8 -*-
"""API v1 任务路由：队列列表、详情、事件分页、取消与手动重试

列表按 keyset 分页并支持 state/task_type/KB/Document 筛选；queued
任务携带队列位次。取消走任务引擎的幂等取消语义（排队/等待确认/
等待重试立即收尾，执行中转等待取消，终态拒绝）；重试仅对失败任务
创建携带父关系的新任务（202）。
"""
import json
from dataclasses import dataclass

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app.domain.entities import Task, TaskStatus
from app.domain.errors import (
    EntityNotFoundError,
    TaskQueueFullError,
    TaskStateConflictError,
)
from app.domain.ports import (
    DocumentRepository,
    DocumentVersionRepository,
    TaskRepository,
)

from .envelope import error_envelope, new_request_id, serialize_task, success_envelope
from .pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor


@dataclass(frozen=True)
class TaskDependencies:
    """任务端点依赖：由应用装配（或测试）构造

    document/version 仓储用于任务绑定资源的展示字段联查（云端确认
    界面需要文件名与大小）
    """

    task_repo: TaskRepository
    document_repo: DocumentRepository | None = None
    version_repo: DocumentVersionRepository | None = None


# 取消冲突与重试冲突的错误码区分（05 合同错误族）
_CANCEL_CONFLICT_CODE = "TASK_NOT_CANCELLABLE"
_RETRY_CONFLICT_CODE = "TASK_STATE_CONFLICT"


def create_tasks_router(deps: TaskDependencies) -> APIRouter:
    """装配任务路由（随 v1 路由挂载，路径前缀由父路由提供）"""
    router = APIRouter()

    @router.get("/tasks")
    def list_tasks(
        request: Request,
        cursor: str | None = None,
        limit: int | None = None,
        state: str | None = None,
        task_type: str | None = None,
        knowledge_base_id: str | None = None,
        document_id: str | None = None,
    ) -> JSONResponse:
        """任务队列视图（keyset 分页；queued 任务携带队列位次）"""
        request_id = new_request_id()
        state_filter = None
        if state is not None:
            try:
                state_filter = TaskStatus(state)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content=error_envelope(
                        request_id, "INVALID_PARAM", f"任务状态取值非法: {state}"
                    ),
                )
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
        items = deps.task_repo.list_tasks(
            states=(state_filter,) if state_filter else None,
            task_type=task_type,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
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
            "items": [
                _task_view(
                    task,
                    queue_position=deps.task_repo.queue_position(task.id),
                )
                for task in items
            ],
            "next_cursor": next_cursor,
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/tasks/{task_id}")
    def get_task(task_id: str) -> JSONResponse:
        """任务详情（含最近 10 条审计事件，按写入顺序）"""
        request_id = new_request_id()
        task = deps.task_repo.get(task_id)
        if task is None:
            return _task_not_found(request_id)
        events = deps.task_repo.list_events(task_id)
        payload = {
            "task": _task_view(
                task, queue_position=deps.task_repo.queue_position(task_id)
            ),
            "recent_events": [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "state": event.state.value,
                    "stage": event.stage.value if event.stage else None,
                    "attempt_count": event.attempt_count,
                    "worker": event.worker,
                    "created_at": event.created_at,
                    "error_code": event.error_code,
                    "detail_json": event.detail_json,
                }
                for event in events[-10:]
            ],
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/tasks/{task_id}/events")
    def list_task_events(
        task_id: str, cursor: str | None = None, limit: int | None = None
    ) -> JSONResponse:
        """任务事件时间线（按写入顺序，keyset 分页）"""
        request_id = new_request_id()
        task = deps.task_repo.get(task_id)
        if task is None:
            return _task_not_found(request_id)
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
        events = deps.task_repo.list_events(task_id)
        # 事件按写入顺序（即创建时间升序）分页：游标为页尾排序键
        if after_created_at is not None:
            events = [
                event
                for event in events
                if (event.created_at, event.id) > (after_created_at, after_id)
            ]
        page = events[:page_size]
        next_cursor = (
            encode_cursor(page[-1].created_at, page[-1].id)
            if len(page) == page_size
            else None
        )
        payload = {
            "items": [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "state": event.state.value,
                    "stage": event.stage.value if event.stage else None,
                    "attempt_count": event.attempt_count,
                    "worker": event.worker,
                    "created_at": event.created_at,
                    "error_code": event.error_code,
                    "detail_json": event.detail_json,
                }
                for event in page
            ],
            "next_cursor": next_cursor,
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.post("/tasks/{task_id}/cancel")
    def cancel_task(
        task_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        """请求取消任务（幂等；执行中转等待取消，排队立即收尾）"""
        request_id = new_request_id()
        try:
            task = deps.task_repo.request_cancel(task_id)
        except EntityNotFoundError:
            return _task_not_found(request_id)
        except TaskStateConflictError as exc:
            return JSONResponse(
                status_code=409,
                content=error_envelope(
                    request_id, _CANCEL_CONFLICT_CODE, str(exc)
                ),
            )
        return JSONResponse(
            status_code=200,
            content=success_envelope(
                {"task": serialize_task(task)}, request_id
            ),
        )

    @router.post("/tasks/{task_id}/retry")
    def retry_task(
        task_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        """手动重试失败任务：创建携带父关系的新任务（202）"""
        request_id = new_request_id()
        try:
            task = deps.task_repo.retry_failed(
                task_id, idempotency_key=idempotency_key
            )
        except EntityNotFoundError:
            return _task_not_found(request_id)
        except TaskStateConflictError as exc:
            return JSONResponse(
                status_code=409,
                content=error_envelope(
                    request_id, _RETRY_CONFLICT_CODE, str(exc)
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

    def _task_not_found(request_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=error_envelope(request_id, "TASK_NOT_FOUND", "任务不存在"),
        )

    def _task_view(task: Task, *, queue_position: int | None) -> dict:
        """任务视图：TaskDTO 增量携带绑定资源的展示字段

        document_display_name / document_size_bytes 供云端确认界面
        展示文件名与大小；route_mode / route_reason 从任务输入的
        路由决策记录解析，供确认界面说明上传原因
        """
        view = serialize_task(task, queue_position=queue_position)
        view["document_display_name"] = None
        view["document_size_bytes"] = None
        view["route_mode"] = None
        view["route_reason"] = None
        if task.input_json:
            try:
                route = json.loads(task.input_json)
                if isinstance(route, dict):
                    view["route_mode"] = route.get("mode")
                    view["route_reason"] = route.get("reason")
            except json.JSONDecodeError:
                pass  # 内部事实破坏时展示字段保持为空，不影响任务本身
        if deps.document_repo is None or task.document_id is None:
            return view
        document = deps.document_repo.get(task.document_id)
        if document is None:
            return view
        view["document_display_name"] = document.display_name
        if deps.version_repo is not None and task.document_version_id is not None:
            version = deps.version_repo.get(task.document_version_id)
            if version is not None:
                view["document_size_bytes"] = version.size_bytes
        return view

    return router
