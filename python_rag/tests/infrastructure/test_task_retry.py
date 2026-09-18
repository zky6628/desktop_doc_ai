# -*- coding: utf-8 -*-
"""任务自动重试、失败终态、取消流程与手动重试测试"""
from datetime import datetime, timedelta, timezone

import pytest

from app.domain import task_state
from app.domain.entities import Task, TaskStage, TaskStatus
from app.domain.errors import (
    EntityNotFoundError,
    TaskLeaseLostError,
    TaskQueueFullError,
    TaskStateConflictError,
)
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import SQLiteTaskRepository

from .schema_helpers import fresh_db


@pytest.fixture()
def task_repo(tmp_path):
    """应用全部迁移的临时库 + 任务仓储与库路径"""
    db_path, _ = fresh_db(tmp_path, name="task_retry.db")
    conn = connect(db_path)
    yield SQLiteTaskRepository(conn), conn, db_path
    conn.close()


def _claimed_task(repo: SQLiteTaskRepository) -> Task:
    """创建并领取一个任务（进入 running，持有租约）"""
    task = repo.create("import")
    claimed = repo.claim_next("worker-1")
    assert claimed is not None
    assert claimed.id == task.id
    return claimed


def _expire_next_retry(conn, task_id: str) -> None:
    """把任务的下次重试时间改到过去（测试中立即到期）"""
    conn.execute(
        "UPDATE tasks SET next_retry_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (task_id,),
    )


def test_retry_backoff_seconds_table():
    """退避表 5/20/60 秒，抖动上限 20%；越界序号拒绝"""
    assert task_state.retry_backoff_seconds(1, rng=lambda: 0.0) == 5
    assert task_state.retry_backoff_seconds(2, rng=lambda: 0.0) == 20
    assert task_state.retry_backoff_seconds(3, rng=lambda: 0.0) == 60
    assert task_state.retry_backoff_seconds(1, rng=lambda: 1.0) == 6
    assert task_state.retry_backoff_seconds(2, rng=lambda: 1.0) == 24
    assert task_state.retry_backoff_seconds(3, rng=lambda: 1.0) == 72
    with pytest.raises(KeyError):
        task_state.retry_backoff_seconds(4)


def test_schedule_retry_parks_task_and_releases_lease(task_repo):
    """安排重试：转入 retry_waiting，计数预增，退避时间写入，租约释放"""
    repo, _, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(claimed.id, "worker-1", stage=TaskStage.VALIDATING)

    retried = repo.schedule_retry(
        claimed.id, "worker-1",
        error_code="NETWORK_INTERRUPTED", error_message="网络中断",
    )
    assert retried.state is TaskStatus.RETRY_WAITING
    assert retried.retry_count == 1
    assert retried.stage_attempt == 2
    assert retried.total_attempt_count == 1
    assert retried.lease_owner is None
    assert retried.lease_expires_at is None
    assert retried.error_code == "NETWORK_INTERRUPTED"
    assert retried.next_retry_at is not None

    # 退避落在 5~6 秒档（秒精度存储，与当前时间留出跨秒余量）
    now = datetime.now(timezone.utc)
    assert retried.next_retry_at > (
        now - timedelta(seconds=1)
    ).isoformat(timespec="seconds")
    assert retried.next_retry_at <= (
        now + timedelta(seconds=8)
    ).isoformat(timespec="seconds")

    events = repo.list_events(claimed.id)
    assert [event.state for event in events] == [
        TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRY_WAITING,
    ]
    assert events[-1].error_code == "NETWORK_INTERRUPTED"
    assert events[-1].worker == "worker-1"


def test_schedule_retry_from_waiting_external(task_repo):
    """等待外部结果的任务可安排重试（该状态有 retry_waiting 出边）"""
    repo, _, _ = task_repo
    claimed = _claimed_task(repo)
    repo.transition(
        claimed.id, TaskStatus.WAITING_EXTERNAL, stage=TaskStage.SUBMITTING_CLOUD
    )

    retried = repo.schedule_retry(claimed.id, "worker-1", error_code="MINERU_UNAVAILABLE")
    assert retried.state is TaskStatus.RETRY_WAITING
    assert retried.stage is TaskStage.SUBMITTING_CLOUD


def test_schedule_retry_requires_valid_lease(task_repo):
    """安排重试守卫：未领取、错手租约均被拒绝"""
    repo, _, _ = task_repo
    unclaimed = repo.create("import")
    claimed = repo.create("import", priority=10)
    assert repo.claim_next("worker-1").id == claimed.id

    with pytest.raises(TaskLeaseLostError):
        repo.schedule_retry(unclaimed.id, "worker-1", error_code="X")
    with pytest.raises(TaskLeaseLostError):
        repo.schedule_retry(claimed.id, "worker-2", error_code="X")


def test_schedule_retry_fails_task_when_stage_budget_exhausted(task_repo):
    """同一阶段三次重试耗尽后：第四次失败直接转入失败终态"""
    repo, conn, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(claimed.id, "worker-1", stage=TaskStage.PARSING_LOCAL)

    for _ in range(3):
        retried = repo.schedule_retry(
            claimed.id, "worker-1", error_code="NETWORK_INTERRUPTED"
        )
        assert retried.state is TaskStatus.RETRY_WAITING
        _expire_next_retry(conn, claimed.id)
        assert repo.promote_due_retries() == 1
        re_claimed = repo.claim_next("worker-1")
        assert re_claimed is not None
        # 重试再执行进入同一阶段：执行序号沿用安排时预增的值
        same_stage = repo.update_stage(
            re_claimed.id, "worker-1", stage=TaskStage.PARSING_LOCAL
        )
        assert same_stage.stage_attempt == retried.stage_attempt

    final = repo.schedule_retry(
        claimed.id, "worker-1", error_code="NETWORK_INTERRUPTED"
    )
    assert final.state is TaskStatus.FAILED
    assert final.retry_count == 3
    assert final.stage_attempt == 4
    assert final.total_attempt_count == 3
    assert final.finished_at is not None
    assert final.lease_owner is None

    states = [event.state for event in repo.list_events(claimed.id)]
    assert states.count(TaskStatus.RETRY_WAITING) == 3
    assert states[-1] is TaskStatus.FAILED


def test_schedule_retry_fails_when_total_attempts_capped(task_repo):
    """累计重试执行达到硬上限 12 后不再安排重试，直接失败"""
    repo, conn, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(claimed.id, "worker-1", stage=TaskStage.CHUNKING)
    conn.execute(
        "UPDATE tasks SET total_attempt_count = 12 WHERE id = ?", (claimed.id,)
    )

    final = repo.schedule_retry(claimed.id, "worker-1", error_code="X")
    assert final.state is TaskStatus.FAILED
    assert final.total_attempt_count == 12
    assert final.retry_count == 0


def test_promote_due_retries_moves_only_due_tasks(task_repo):
    """提升到期重试：仅到期的任务回排队，未到期保持等待"""
    repo, conn, _ = task_repo
    due = _claimed_task(repo)
    repo.update_stage(due.id, "worker-1", stage=TaskStage.VALIDATING)
    repo.schedule_retry(due.id, "worker-1", error_code="X")

    waiting = _claimed_task(repo)
    repo.update_stage(waiting.id, "worker-1", stage=TaskStage.CHUNKING)
    repo.schedule_retry(waiting.id, "worker-1", error_code="X")

    _expire_next_retry(conn, due.id)
    assert repo.promote_due_retries() == 1

    promoted = repo.get(due.id)
    assert promoted.state is TaskStatus.QUEUED
    assert promoted.next_retry_at is None
    # 当前阶段的重试预算保留，重试再执行继续按序累计
    assert promoted.retry_count == 1
    assert promoted.stage_attempt == 2

    still_waiting = repo.get(waiting.id)
    assert still_waiting.state is TaskStatus.RETRY_WAITING
    assert still_waiting.next_retry_at is not None


def test_fail_task_marks_terminal_and_releases_lease(task_repo):
    """不可恢复错误：转入失败终态，回填错误与完成时间，释放租约"""
    repo, _, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(
        claimed.id, "worker-1", stage=TaskStage.STORING_FILE, checkpoint_json='{"stored": "staging"}'
    )

    failed = repo.fail_task(
        claimed.id, "worker-1",
        error_code="UNSUPPORTED_FORMAT", error_message="不支持的文件格式",
    )
    assert failed.state is TaskStatus.FAILED
    assert failed.error_code == "UNSUPPORTED_FORMAT"
    assert failed.error_message == "不支持的文件格式"
    assert failed.finished_at is not None
    assert failed.lease_owner is None

    events = repo.list_events(claimed.id)
    assert events[-1].state is TaskStatus.FAILED
    assert events[-1].error_code == "UNSUPPORTED_FORMAT"
    assert events[-1].checkpoint_json == '{"stored": "staging"}'


def test_fail_task_requires_valid_lease(task_repo):
    """失败写入守卫：无租约与错手租约均被拒绝"""
    repo, _, _ = task_repo
    unclaimed = repo.create("import")
    claimed = repo.create("import", priority=10)
    assert repo.claim_next("worker-1").id == claimed.id

    with pytest.raises(TaskLeaseLostError):
        repo.fail_task(unclaimed.id, "worker-1", error_code="X")
    with pytest.raises(TaskLeaseLostError):
        repo.fail_task(claimed.id, "worker-2", error_code="X")


def test_request_cancel_immediately_cancels_queued(task_repo):
    """排队中的任务取消：立即进入 cancelled 终态"""
    repo, _, _ = task_repo
    task = repo.create("import")

    cancelled = repo.request_cancel(task.id)
    assert cancelled.state is TaskStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert cancelled.cancel_requested_at is None

    events = repo.list_events(task.id)
    assert [event.state for event in events] == [
        TaskStatus.QUEUED, TaskStatus.CANCELLED,
    ]


def test_request_cancel_running_waits_for_checkpoint(task_repo):
    """执行中的任务取消：先转 cancel_requested 并保留租约，重复请求幂等"""
    repo, _, _ = task_repo
    claimed = _claimed_task(repo)

    requested = repo.request_cancel(claimed.id)
    assert requested.state is TaskStatus.CANCEL_REQUESTED
    assert requested.cancel_requested_at is not None
    assert requested.lease_owner == "worker-1"
    assert requested.finished_at is None

    again = repo.request_cancel(claimed.id)
    assert again.state is TaskStatus.CANCEL_REQUESTED
    assert again.cancel_requested_at == requested.cancel_requested_at


def test_request_cancel_from_retry_waiting_and_terminal_rejected(task_repo):
    """等待重试的任务可请求取消（Worker 到检查点收尾）；终态任务不可取消"""
    repo, _, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(claimed.id, "worker-1", stage=TaskStage.VALIDATING)
    repo.schedule_retry(claimed.id, "worker-1", error_code="X")

    requested = repo.request_cancel(claimed.id)
    assert requested.state is TaskStatus.CANCEL_REQUESTED

    succeeded = _claimed_task(repo)
    repo.transition(succeeded.id, TaskStatus.SUCCEEDED, stage=TaskStage.COMPLETED)
    with pytest.raises(TaskStateConflictError):
        repo.request_cancel(succeeded.id)


def test_cancel_at_checkpoint_completes_cancellation(task_repo):
    """检查点取消：Worker 确认后转 cancelled、释放租约并记录事件"""
    repo, _, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(
        claimed.id, "worker-1", stage=TaskStage.CHUNKING, checkpoint_json='{"cursor": 3}'
    )
    repo.request_cancel(claimed.id)

    cancelled = repo.cancel_at_checkpoint(claimed.id, "worker-1")
    assert cancelled.state is TaskStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert cancelled.lease_owner is None
    assert cancelled.cancel_requested_at is not None

    events = repo.list_events(claimed.id)
    assert events[-1].state is TaskStatus.CANCELLED
    assert events[-1].worker == "worker-1"
    assert events[-1].checkpoint_json == '{"cursor": 3}'


def test_cancel_at_checkpoint_requires_waiting_state_and_lease(task_repo):
    """检查点取消守卫：非等待取消状态与错手租约均被拒绝"""
    repo, _, _ = task_repo
    running = _claimed_task(repo)

    # 尚未请求取消：不在等待取消状态
    with pytest.raises(TaskStateConflictError):
        repo.cancel_at_checkpoint(running.id, "worker-1")

    waiting = _claimed_task(repo)
    repo.request_cancel(waiting.id)
    with pytest.raises(TaskLeaseLostError):
        repo.cancel_at_checkpoint(waiting.id, "worker-2")


def test_retry_failed_creates_child_task(task_repo):
    """手动重试：创建派生新任务并继承上下文，原任务保持终态"""
    repo, _, _ = task_repo
    task = repo.create("import", priority=5, input_json='{"file": "报告.pdf"}')
    claimed = repo.claim_next("worker-1")
    assert claimed.id == task.id
    failed = repo.fail_task(claimed.id, "worker-1", error_code="X")

    retried = repo.retry_failed(failed.id)
    assert retried.id != failed.id
    assert retried.state is TaskStatus.QUEUED
    assert retried.parent_task_id == failed.id
    assert retried.retry_origin == "manual"
    assert retried.task_type == failed.task_type
    assert retried.priority == 5
    assert retried.input_json == '{"file": "报告.pdf"}'
    assert retried.max_retries == failed.max_retries
    assert (retried.retry_count, retried.stage_attempt, retried.total_attempt_count) == (0, 0, 0)

    # 原任务保持终态不复活
    assert repo.get(failed.id).state is TaskStatus.FAILED


def test_retry_failed_rejects_non_failed_and_missing(task_repo):
    """非失败状态与不存在的任务不可手动重试"""
    repo, _, _ = task_repo
    queued = repo.create("import")

    with pytest.raises(TaskStateConflictError):
        repo.retry_failed(queued.id)
    with pytest.raises(EntityNotFoundError):
        repo.retry_failed("01900000-0000-7000-8000-000000000000")


def test_retry_failed_idempotency_key_replay(task_repo):
    """手动重试携带幂等键：重放返回同一个新任务"""
    repo, _, _ = task_repo
    task = repo.create("import")
    failed = repo.fail_task(
        repo.claim_next("worker-1").id, "worker-1", error_code="X"
    )
    assert failed.id == task.id

    first = repo.retry_failed(task.id, idempotency_key="manual-1")
    replay = repo.retry_failed(task.id, idempotency_key="manual-1")
    assert replay.id == first.id


def test_retry_failed_respects_queue_capacity(task_repo):
    """队列已满时手动重试被拒绝，原任务保持失败终态"""
    repo, _, _ = task_repo
    # 高优先级保证领取目标确定（同秒创建的任务无可靠先后序）
    victim = repo.create("import", priority=10)
    for _ in range(49):
        repo.create("import")

    claimed = repo.claim_next("worker-1")
    assert claimed.id == victim.id
    failed = repo.fail_task(claimed.id, "worker-1", error_code="X")
    # 失败释放 pending 名额后补一个排队任务，回到 50 上限
    repo.create("import")

    with pytest.raises(TaskQueueFullError):
        repo.retry_failed(failed.id)
    assert repo.get(failed.id).state is TaskStatus.FAILED


def test_retry_cycle_counts_across_stages(task_repo):
    """跨阶段计数语义：首执行不计入累计重试，阶段成功后预算重置"""
    repo, conn, _ = task_repo
    claimed = _claimed_task(repo)

    # 第一阶段失败一次并重试成功
    repo.update_stage(claimed.id, "worker-1", stage=TaskStage.VALIDATING)
    retried = repo.schedule_retry(claimed.id, "worker-1", error_code="X")
    assert retried.total_attempt_count == 1
    _expire_next_retry(conn, claimed.id)
    repo.promote_due_retries()
    re_claimed = repo.claim_next("worker-1")
    repo.complete_stage(re_claimed.id, "worker-1", stage=TaskStage.VALIDATING)

    # 第二阶段首执行：阶段序号与重试预算归零，累计重试保留历史
    next_stage = repo.update_stage(re_claimed.id, "worker-1", stage=TaskStage.CHUNKING)
    assert next_stage.stage_attempt == 1
    assert next_stage.retry_count == 0
    assert next_stage.total_attempt_count == 1
