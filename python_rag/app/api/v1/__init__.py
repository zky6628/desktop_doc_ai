# -*- coding: utf-8 -*-
"""API v1 路由：批量上传与云端确认的最小端点集

端点以闭包持有编排器与仓储（运行时由应用装配注入，测试可替换），
响应统一走 v1 信封。错误码与 HTTP 状态码对齐接口合同；上传为
批次长操作，逐文件独立接受或拒绝，返回 202。
"""
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.domain import file_policy, parser_routing
from app.domain.errors import (
    ConfirmationConflictError,
    EntityNotFoundError,
    TaskStateConflictError,
)
from app.domain.ingest import DUPLICATE_POLICY_VALUES
from app.domain.ports import TaskRepository
from app.infrastructure.ingest import (
    ImportFileInput,
    ImportOrchestrator,
    confirm_cloud_parsing,
)

from .envelope import error_envelope, new_request_id, serialize_task, success_envelope

# 单批次文件数上限复用上传策略常量，保证口径一致
_MAX_BATCH_FILES = file_policy.MAX_BATCH_FILES


def _chunk_reader(fp, chunk_size: int):
    """把同步文件对象包装为按块读取的字节迭代器

    :param fp: 已定位到起始位置的二进制文件对象（Starlette 暂存文件）
    :param chunk_size: 块大小（字节）
    :return: 读到文件末尾（空块）即停止的迭代器
    """
    return iter(lambda: fp.read(chunk_size), b"")


@dataclass(frozen=True)
class ApiV1Dependencies:
    """v1 端点依赖：由应用装配（或测试）构造"""

    orchestrator: ImportOrchestrator
    task_repo: TaskRepository


class CloudConfirmationRequest(BaseModel):
    """云端确认请求体"""

    decision: str


def create_api_router(deps: ApiV1Dependencies) -> APIRouter:
    """装配 v1 路由

    :param deps: 端点依赖（编排器与任务仓储）
    :return: 挂载到应用上的 APIRouter（前缀 /api/v1）
    """

    router = APIRouter(prefix="/api/v1")

    @router.post("/knowledge-bases/{kb_id}/documents")
    def upload_documents(
        kb_id: str,
        request: Request,
        files: Annotated[list[UploadFile], File(...)],
        parser_preference: Annotated[str, Form()] = "auto",
        duplicate_policy: Annotated[str, Form()] = "skip",
    ) -> JSONResponse:
        """multipart 批量上传：暂存、路由并逐文件建立版本与导入任务"""
        request_id = new_request_id()
        if parser_preference not in parser_routing.PARSER_PREFERENCE_VALUES:
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    request_id,
                    "INVALID_PARAM",
                    f"parser_preference 取值非法: {parser_preference}",
                ),
            )
        if duplicate_policy not in DUPLICATE_POLICY_VALUES:
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    request_id,
                    "INVALID_PARAM",
                    f"duplicate_policy 取值非法: {duplicate_policy}",
                ),
            )
        if len(files) > _MAX_BATCH_FILES:
            return JSONResponse(
                status_code=413,
                content=error_envelope(
                    request_id,
                    "BATCH_TOO_LARGE",
                    f"单批次文件数不能超过 {_MAX_BATCH_FILES}",
                ),
            )

        # 幂等键按批次键 + 文件序号派生，落到任务幂等键列；
        # 批次组成变化会改变派生键，重放语义以同批次重提为准
        batch_key = request.headers.get("Idempotency-Key")

        inputs = [
            ImportFileInput(
                display_name=file.filename or "",
                declared_mime=file.content_type,
                chunks=_chunk_reader(
                    file.file, file_policy.STREAM_CHUNK_BYTES
                ),
                idempotency_key=(
                    None
                    if batch_key is None
                    else f"{batch_key}:{index}"
                ),
            )
            for index, file in enumerate(files)
        ]
        results = deps.orchestrator.import_batch(
            kb_id,
            inputs,
            duplicate_policy=duplicate_policy,
            parser_preference=parser_preference,
        )
        payload = {
            "results": [
                {
                    "display_name": result.display_name,
                    "accepted": result.accepted,
                    "document_id": result.document_id,
                    "document_version_id": result.document_version_id,
                    "task_id": result.task_id,
                    "task_state": result.task_state,
                    "route": (
                        None
                        if result.route_mode is None
                        else {
                            "mode": result.route_mode,
                            "reason": result.route_reason,
                        }
                    ),
                    "error": (
                        None
                        if result.error_code is None
                        else {
                            "code": result.error_code,
                            "message": result.error_message,
                        }
                    ),
                }
                for result in results
            ]
        }
        return JSONResponse(
            status_code=202,
            content=success_envelope(payload, request_id),
        )

    @router.post("/tasks/{task_id}/cloud-confirmation")
    def cloud_confirmation(
        task_id: str, body: CloudConfirmationRequest
    ) -> JSONResponse:
        """对等待确认的任务执行批准（继续云端解析）或拒绝（取消）"""
        request_id = new_request_id()
        try:
            task = confirm_cloud_parsing(deps.task_repo, task_id, body.decision)
        except EntityNotFoundError:
            return JSONResponse(
                status_code=404,
                content=error_envelope(request_id, "TASK_NOT_FOUND", "任务不存在"),
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content=error_envelope(request_id, "INVALID_PARAM", str(exc)),
            )
        except (ConfirmationConflictError, TaskStateConflictError) as exc:
            # 状态预检与迁移之间的竞态同样按确认冲突表达
            return JSONResponse(
                status_code=409,
                content=error_envelope(
                    request_id, "CONFIRMATION_CONFLICT", str(exc)
                ),
            )
        return JSONResponse(
            status_code=200,
            content=success_envelope({"task": serialize_task(task)}, request_id),
        )

    return router


__all__ = [
    "ApiV1Dependencies",
    "CloudConfirmationRequest",
    "create_api_router",
]
