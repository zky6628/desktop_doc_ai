# -*- coding: utf-8 -*-
"""API v1 统一信封与响应工具

成功与错误响应统一携带 request_id（服务端生成的 UUIDv7，用于
日志与问题排查关联）；错误响应额外提供可重试性标注。任务序列化
输出接口层最小 TaskDTO 字段集。
"""
from app.domain.entities import Task, TaskStatus
from app.domain.ids import uuid7

# 任务终态：非终态任务可请求取消，仅失败任务可手动重试
_TERMINAL_STATES = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def new_request_id() -> str:
    """生成响应关联用的请求 ID"""
    return uuid7()


def success_envelope(data, request_id: str) -> dict:
    """构造成功响应信封

    :param data: 业务数据
    :param request_id: 请求 ID
    """
    return {"success": True, "request_id": request_id, "data": data, "error": None}


def error_envelope(
    request_id: str,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> dict:
    """构造错误响应信封

    :param request_id: 请求 ID
    :param code: 稳定错误码
    :param message: 用户可读的错误说明（不含敏感信息）
    :param retryable: 客户端是否可原样重试
    """
    return {
        "success": False,
        "request_id": request_id,
        "data": None,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def serialize_citation(record) -> dict:
    """把引用快照记录序列化为 CitationDTO 视图

    SSE citation 事件、查询聚合与会话历史消息共用同一形状；citation_order
    即回答正文 [S编号] 的编号，供客户端内联定位。
    """
    return {
        "id": record.citation_id,
        "citation_order": record.citation_order,
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


def serialize_task(task: Task, *, queue_position: int | None = None) -> dict:
    """把任务实体序列化为接口层最小 TaskDTO

    取消/重试能力标注由任务状态推导：非终态可取消（取消请求幂等），
    仅失败任务可手动重试；queue_position 由调用方按队列口径计算后
    传入（非排队状态为 None）。错误信息已在产生侧完成脱敏，此处
    原样透出。

    :param task: 任务实体
    :param queue_position: 队列位次（queued 状态专用）
    :return: TaskDTO 字典
    """
    return {
        "id": task.id,
        "task_type": task.task_type,
        "state": task.state.value,
        "stage": task.stage.value if task.stage is not None else None,
        "progress": task.progress,
        "queue_position": queue_position,
        "cancellable": task.state not in _TERMINAL_STATES,
        "retryable": task.state is TaskStatus.FAILED,
        "retry_count": task.retry_count,
        "stage_attempt": task.stage_attempt,
        "total_attempt_count": task.total_attempt_count,
        "max_retries": task.max_retries,
        "knowledge_base_id": task.knowledge_base_id,
        "document_id": task.document_id,
        "document_version_id": task.document_version_id,
        "parent_task_id": task.parent_task_id,
        "error": (
            None
            if task.error_code is None
            else {"code": task.error_code, "message": task.error_message}
        ),
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }
