# -*- coding: utf-8 -*-
"""API v1 统一信封与响应工具

成功与错误响应统一携带 request_id（服务端生成的 UUIDv7，用于
日志与问题排查关联）；错误响应额外提供可重试性标注。任务序列化
输出接口层最小 TaskDTO 字段集。
"""
from app.domain.entities import Task
from app.domain.ids import uuid7


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


def serialize_task(task: Task) -> dict:
    """把任务实体序列化为接口层最小 TaskDTO

    取消/重试能力标注随任务中心端点一并补充；错误信息已在产生侧
    完成脱敏，此处原样透出。

    :param task: 任务实体
    :return: TaskDTO 字典
    """
    return {
        "id": task.id,
        "task_type": task.task_type,
        "state": task.state.value,
        "stage": task.stage.value if task.stage is not None else None,
        "progress": task.progress,
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
