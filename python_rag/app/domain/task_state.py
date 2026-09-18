# -*- coding: utf-8 -*-
"""任务状态机与队列容量合同

状态生命周期与处理阶段分离：state 描述任务生命周期，stage 描述当前或
最近处理阶段，阶段名不得写入 state。合法迁移关系、终态/pending 口径与
容量上限在此统一定义；仓储实现与后续的领取/调度逻辑必须复用本模块，
禁止各自内联规则导致口径漂移。
"""
from app.domain.entities import TaskStatus
from app.domain.errors import TaskStateConflictError

# 当前状态 -> 允许迁移到的状态集合；未列出的状态（终态）无出边，
# 手动重试通过创建新任务表达，不允许从终态原地复活
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.WAITING_USER: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.QUEUED,
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_EXTERNAL,
        TaskStatus.RETRY_WAITING,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
    }),
    TaskStatus.WAITING_EXTERNAL: frozenset({
        TaskStatus.QUEUED,
        TaskStatus.RETRY_WAITING,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.FAILED,
    }),
    TaskStatus.RETRY_WAITING: frozenset({
        TaskStatus.QUEUED,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.FAILED,
    }),
    TaskStatus.CANCEL_REQUESTED: frozenset({
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    }),
}

# 终态：不可迁移；等待用户确认等中间态不在此列
TERMINAL_STATES = frozenset({
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
})

# pending 容量口径：等待推进的状态合计，含等待取消的 cancel_requested
PENDING_STATES = frozenset({
    TaskStatus.QUEUED,
    TaskStatus.WAITING_USER,
    TaskStatus.WAITING_EXTERNAL,
    TaskStatus.RETRY_WAITING,
    TaskStatus.CANCEL_REQUESTED,
})

# 队列容量上限：物理执行 3、pending 合计 50、非终态合计 53。
# running 的物理执行许可由领取逻辑做租约感知计数（等待取消的任务
# 在释放租约前仍占执行许可），任务创建时只校验 pending 与非终态合计
MAX_RUNNING = 3
MAX_PENDING = 50
MAX_NON_TERMINAL = 53

# Worker 租约固定参数：租约时长、心跳间隔与接管宽限。
# 心跳间隔由 Worker 侧遵守（远小于租约时长，正常执行不会失约）；
# 接管宽限供重启恢复判定过期任务何时可被重新排队
LEASE_DURATION_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 15
TAKEOVER_GRACE_SECONDS = 30


def require_valid_transition(current: TaskStatus, target: TaskStatus) -> None:
    """校验状态迁移合法性，非法时抛出状态冲突错误

    :param current: 当前状态
    :param target: 目标状态
    :raises TaskStateConflictError: 迁移不在允许表中（含终态出边与自迁移）
    """
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise TaskStateConflictError(
            f"非法状态迁移: {current.value} -> {target.value}"
        )
