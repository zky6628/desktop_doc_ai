# -*- coding: utf-8 -*-
"""云端解析确认服务：waiting_user 任务的批准与拒绝

确认是用户对路由升级建议的答复：仅处于等待确认状态（且停留在
路由阶段）的任务可被确认。批准使任务回到排队并进入云端提交阶段；
拒绝即放弃本次云端解析，任务直接取消收尾。确认答复的语义与任务
状态机共用同一套迁移实现，确认冲突以专门错误类型表达。
"""
from app.domain.entities import Task, TaskStage, TaskStatus
from app.domain.errors import ConfirmationConflictError, EntityNotFoundError
from app.domain.ports import TaskRepository

# 用户可见的确认答复取值
CLOUD_CONFIRMATION_DECISIONS = ("approve", "reject")

# 批准后进入的处理阶段：云端提交由其适配器接管（随云端适配器接入）
_APPROVED_STAGE = TaskStage.SUBMITTING_CLOUD


def confirm_cloud_parsing(
    task_repo: TaskRepository, task_id: str, decision: str
) -> Task:
    """对等待确认的任务执行批准或拒绝

    :param task_repo: 任务仓储
    :param task_id: 任务 ID
    :param decision: 确认答复（approve/reject）
    :return: 确认后的任务（queued 或 cancelled）
    :raises ValueError: 答复取值非法
    :raises EntityNotFoundError: 任务不存在
    :raises ConfirmationConflictError: 任务不在等待确认状态或已不在路由阶段
    """
    if decision not in CLOUD_CONFIRMATION_DECISIONS:
        raise ValueError(f"非法确认答复: {decision}")

    task = task_repo.get(task_id)
    if task is None:
        raise EntityNotFoundError(f"任务不存在: {task_id}")
    if (
        task.state is not TaskStatus.WAITING_USER
        or task.stage is not TaskStage.ROUTING_PARSER
    ):
        raise ConfirmationConflictError(
            "任务不在等待云端解析确认状态，无法确认"
        )

    if decision == "approve":
        return task_repo.transition(
            task_id, TaskStatus.QUEUED, stage=_APPROVED_STAGE
        )
    # 拒绝即放弃云端解析：等待确认的任务尚未执行，直接取消收尾
    return task_repo.request_cancel(task_id)
