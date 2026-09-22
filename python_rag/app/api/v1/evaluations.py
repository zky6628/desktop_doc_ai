# -*- coding: utf-8 -*-
"""API v1 评测路由：切片参数对比评测的创建、查询与取消

创建为编排长操作：预检（KB 可用、参数组合法、切片规模可控、无运
行中评测）后建立运行记录与编排任务，202 返回；进度与对比结果经
运行记录轮询读取。评测独占在役切片参数，同一时刻至多一个运行中
评测。取消复用任务取消语义（任务收尾后由 Worker 入队恢复重建，
参数与索引不留实验状态）。
"""
import sqlite3
from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.domain.chunking import validate_chunking_params
from app.domain.entities import EvaluationRunState
from app.domain.errors import EntityNotFoundError, TaskQueueFullError
from app.domain.ports import (
    EvaluationRunRepository,
    KnowledgeBaseRepository,
    TaskRepository,
)
from app.infrastructure.evaluation import (
    MAX_EVALUATION_CHUNKS,
    EvaluationRunService,
)

from .envelope import error_envelope, new_request_id, success_envelope

# 参数组与问题集的规模上限（对比表可读性与任务时长边界）
_MAX_PARAM_GROUPS = 5
_MAX_QUESTIONS = 200


def serialize_evaluation_run(run) -> dict:
    """评测运行的对外序列化（含进度与对比结果）"""
    return {
        "id": run.id,
        "knowledge_base_id": run.knowledge_base_id,
        "task_id": run.task_id,
        "state": run.state.value,
        "target_version_ids": list(run.target_version_ids),
        "questions": list(run.questions),
        "param_groups": [dict(group) for group in run.param_groups],
        "progress": {
            "current_group_index": run.current_group_index,
            "current_question_index": run.current_question_index,
        },
        "results": run.results,
        "error_code": run.error_code,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


@dataclass(frozen=True)
class EvaluationDependencies:
    """评测端点依赖：由应用装配（或测试）构造"""

    conn: sqlite3.Connection
    kb_repo: KnowledgeBaseRepository
    task_repo: TaskRepository
    evaluation_repo: EvaluationRunRepository
    evaluation_service: EvaluationRunService


class EvaluationRunBody(BaseModel):
    """评测运行创建请求体"""

    knowledge_base_id: str
    questions: list[str]
    param_groups: list[dict]


def create_evaluations_router(deps: EvaluationDependencies) -> APIRouter:
    """装配评测路由（随 v1 路由挂载，路径前缀由父路由提供）"""
    router = APIRouter()

    @router.post("/evaluation-runs")
    def create_evaluation_run(body: EvaluationRunBody) -> JSONResponse:
        """创建评测运行：预检后登记运行记录与编排任务（202）"""
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
        questions = [question.strip() for question in body.questions]
        questions = [question for question in questions if question]
        if not questions:
            return JSONResponse(
                status_code=422,
                content=error_envelope(
                    request_id, "INVALID_PARAM", "问题集不能为空"
                ),
            )
        if len(questions) > _MAX_QUESTIONS:
            return JSONResponse(
                status_code=422,
                content=error_envelope(
                    request_id,
                    "INVALID_PARAM",
                    f"问题数不能超过 {_MAX_QUESTIONS}",
                ),
            )
        if not body.param_groups:
            return JSONResponse(
                status_code=422,
                content=error_envelope(
                    request_id, "INVALID_PARAM", "参数组不能为空"
                ),
            )
        if len(body.param_groups) > _MAX_PARAM_GROUPS:
            return JSONResponse(
                status_code=422,
                content=error_envelope(
                    request_id,
                    "INVALID_PARAM",
                    f"参数组数不能超过 {_MAX_PARAM_GROUPS}",
                ),
            )
        try:
            groups = [
                validate_chunking_params(
                    group.get("parent_chunk_chars"),
                    group.get("child_chunk_chars"),
                )
                for group in body.param_groups
            ]
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content=error_envelope(request_id, "INVALID_PARAM", str(exc)),
            )
        if deps.evaluation_repo.get_running() is not None:
            return JSONResponse(
                status_code=409,
                content=error_envelope(
                    request_id, "EVALUATION_RUNNING", "已有评测运行进行中"
                ),
            )

        target_version_ids = deps.evaluation_service.version_ids_for_kb(
            body.knowledge_base_id
        )
        if not target_version_ids:
            return JSONResponse(
                status_code=422,
                content=error_envelope(
                    request_id, "INVALID_PARAM", "知识库无可评测的文档版本"
                ),
            )
        # 规模预检：取切片数最多的参数组（子预算最小）估算总量
        largest_group = min(groups, key=lambda params: params.child_chunk_chars)
        estimated = deps.evaluation_service.estimate_chunk_count(
            target_version_ids, largest_group
        )
        if estimated > MAX_EVALUATION_CHUNKS:
            return JSONResponse(
                status_code=422,
                content=error_envelope(
                    request_id,
                    "EVALUATION_TOO_LARGE",
                    f"预估切片总量 {estimated} 超过上限 {MAX_EVALUATION_CHUNKS}",
                ),
            )
        try:
            task = deps.task_repo.create(
                "evaluation_run", knowledge_base_id=body.knowledge_base_id
            )
        except TaskQueueFullError:
            return JSONResponse(
                status_code=429,
                content=error_envelope(
                    request_id, "TASK_QUEUE_FULL", "任务队列已满"
                ),
            )
        run = deps.evaluation_repo.create(
            knowledge_base_id=body.knowledge_base_id,
            task_id=task.id,
            target_version_ids=target_version_ids,
            questions=questions,
            param_groups=[
                {
                    "parent_chunk_chars": params.parent_chunk_chars,
                    "child_chunk_chars": params.child_chunk_chars,
                }
                for params in groups
            ],
        )
        return JSONResponse(
            status_code=202,
            content=success_envelope(
                serialize_evaluation_run(run), request_id
            ),
        )

    @router.get("/evaluation-runs")
    def list_evaluation_runs(kb_id: str) -> JSONResponse:
        """按知识库列出评测运行（创建时间倒序）"""
        request_id = new_request_id()
        runs = deps.evaluation_repo.list_by_knowledge_base(kb_id)
        payload = {
            "items": [serialize_evaluation_run(run) for run in runs],
            "next_cursor": None,
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/evaluation-runs/{run_id}")
    def get_evaluation_run(run_id: str) -> JSONResponse:
        """读取评测运行：状态、进度与各组对比结果"""
        request_id = new_request_id()
        run = deps.evaluation_repo.get(run_id)
        if run is None:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "EVALUATION_NOT_FOUND", "评测运行不存在"
                ),
            )
        return JSONResponse(
            status_code=200,
            content=success_envelope(serialize_evaluation_run(run), request_id),
        )

    @router.post("/evaluation-runs/{run_id}/cancel")
    def cancel_evaluation_run(run_id: str) -> JSONResponse:
        """请求取消评测运行（幂等；恢复重建由 Worker 收尾后入队）"""
        request_id = new_request_id()
        run = deps.evaluation_repo.get(run_id)
        if run is None:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "EVALUATION_NOT_FOUND", "评测运行不存在"
                ),
            )
        try:
            cancelled_task = deps.task_repo.request_cancel(run.task_id)
        except EntityNotFoundError:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "TASK_NOT_FOUND", "编排任务不存在"
                ),
            )
        # 排队中的任务取消立即收尾：运行记录同步终态（在役参数未被
        # 评测动过，无需恢复重建；运行中任务由 Worker 检查点收尾）
        if cancelled_task.state.value == "cancelled":
            deps.evaluation_repo.mark_terminal(
                run.id, EvaluationRunState.CANCELLED
            )
        return JSONResponse(
            status_code=200,
            content=success_envelope(
                serialize_evaluation_run(deps.evaluation_repo.get(run_id)),
                request_id,
            ),
        )

    return router
