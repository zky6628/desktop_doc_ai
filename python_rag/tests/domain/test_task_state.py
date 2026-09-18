# -*- coding: utf-8 -*-
"""任务状态机纯领域规则测试：迁移表、终态口径、pending 口径与容量常量"""
import pytest

from app.domain import task_state
from app.domain.entities import TaskStage, TaskStatus
from app.domain.errors import TaskStateConflictError

# 状态机允许的全部迁移（当前 -> 目标集合），独立复述领域规则以便对照
_ALLOWED = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.WAITING_USER: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.QUEUED,
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_EXTERNAL,
        TaskStatus.RETRY_WAITING,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_EXTERNAL: {
        TaskStatus.QUEUED,
        TaskStatus.RETRY_WAITING,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.FAILED,
    },
    TaskStatus.RETRY_WAITING: {
        TaskStatus.QUEUED,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.FAILED,
    },
    TaskStatus.CANCEL_REQUESTED: {TaskStatus.CANCELLED, TaskStatus.FAILED},
}


def test_every_allowed_transition_passes():
    """迁移表中每一条合法迁移都通过校验"""
    for current, targets in _ALLOWED.items():
        for target in targets:
            # 不抛异常即通过
            task_state.require_valid_transition(current, target)


def test_every_illegal_transition_rejected():
    """除迁移表外的一切状态对全部拒绝：含终态出边、自迁移与跨支跳跃"""
    all_states = set(TaskStatus)
    for current in all_states:
        for target in all_states:
            if target in _ALLOWED.get(current, set()):
                continue
            with pytest.raises(TaskStateConflictError):
                task_state.require_valid_transition(current, target)


def test_terminal_states_have_no_outgoing_edges():
    """三个终态不在迁移表键中，任何目标状态都被拒绝"""
    assert not (set(task_state.TERMINAL_STATES) & set(task_state.ALLOWED_TRANSITIONS))
    for terminal in task_state.TERMINAL_STATES:
        for target in set(TaskStatus):
            with pytest.raises(TaskStateConflictError):
                task_state.require_valid_transition(terminal, target)


def test_status_and_stage_value_sets():
    """状态 9 个取值、阶段 16 个取值，与状态/阶段定义一一对应"""
    assert {state.value for state in TaskStatus} == {
        "queued", "waiting_user", "running", "waiting_external",
        "retry_waiting", "cancel_requested", "succeeded", "failed", "cancelled",
    }
    assert {stage.value for stage in TaskStage} == {
        "validating", "storing_file", "routing_parser", "parsing_local",
        "submitting_cloud", "polling_cloud", "downloading_cloud_result",
        "normalizing", "chunking", "embedding",
        "writing_vector_index", "writing_keyword_index", "validating_index",
        "activating_version", "cleaning_up", "completed",
    }


def test_capacity_constants():
    """容量常量为 3/50/53"""
    assert task_state.MAX_RUNNING == 3
    assert task_state.MAX_PENDING == 50
    assert task_state.MAX_NON_TERMINAL == 53


def test_pending_and_terminal_state_sets():
    """pending 口径为五个等待态（含等待取消），终态为三个收尾态；
    两者不重叠，且 pending 不含 running"""
    assert task_state.PENDING_STATES == frozenset({
        TaskStatus.QUEUED,
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_EXTERNAL,
        TaskStatus.RETRY_WAITING,
        TaskStatus.CANCEL_REQUESTED,
    })
    assert task_state.TERMINAL_STATES == frozenset({
        TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED,
    })
    assert not (task_state.PENDING_STATES & task_state.TERMINAL_STATES)
    assert TaskStatus.RUNNING not in task_state.PENDING_STATES
