# -*- coding: utf-8 -*-
"""任务重启恢复测试：失效执行重排、失效取消收尾与全状态恢复编排"""
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.entities import Task, TaskStage, TaskStatus
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import SQLiteTaskRepository

from .schema_helpers import fresh_db


@pytest.fixture()
def task_repo(tmp_path):
    """应用全部迁移的临时库 + 任务仓储与库路径"""
    db_path, _ = fresh_db(tmp_path, name="task_recovery.db")
    conn = connect(db_path)
    yield SQLiteTaskRepository(conn), conn, db_path
    conn.close()


def _claimed_task(repo: SQLiteTaskRepository, priority: int = 0) -> Task:
    """创建并领取一个任务（进入 running，持有租约）"""
    task = repo.create("import", priority=priority)
    claimed = repo.claim_next("worker-1")
    assert claimed is not None
    assert claimed.id == task.id
    return claimed


def _backdate_lease(conn, task_id: str, seconds_ago: int) -> None:
    """把租约到期时间改到过去指定秒数（构造失效/宽限内租约）"""
    expires_at = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    ).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE tasks SET lease_expires_at = ? WHERE id = ?", (expires_at, task_id)
    )


def test_stale_running_requeued_with_checkpoint_preserved(task_repo):
    """失效执行重排：仅清租约转排队，阶段/进度/checkpoint/计数全部保留"""
    repo, conn, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(
        claimed.id, "worker-1",
        stage=TaskStage.PARSING_LOCAL,
        progress=0.4,
        checkpoint_json='{"cursor": 7}',
    )
    _backdate_lease(conn, claimed.id, seconds_ago=31)

    assert repo.requeue_stale_running() == 1

    loaded = repo.get(claimed.id)
    assert loaded.state is TaskStatus.QUEUED
    assert loaded.lease_owner is None
    assert loaded.lease_expires_at is None
    assert loaded.stage is TaskStage.PARSING_LOCAL
    assert loaded.progress == 0.4
    assert loaded.checkpoint_json == '{"cursor": 7}'
    assert loaded.attempt_count == 1
    assert loaded.stage_attempt == 1
    assert loaded.total_attempt_count == 0

    events = repo.list_events(claimed.id)
    assert events[-1].state is TaskStatus.QUEUED
    assert events[-1].checkpoint_json == '{"cursor": 7}'
    assert '"interrupted": true' in events[-1].detail_json
    assert '"lease_owner": "worker-1"' in events[-1].detail_json


def test_running_within_grace_or_valid_lease_kept(task_repo):
    """接管宽限内或租约未过期的执行中任务不被重排"""
    repo, conn, _ = task_repo
    valid = _claimed_task(repo)
    assert repo.requeue_stale_running() == 0
    assert repo.get(valid.id).state is TaskStatus.RUNNING

    repo.update_stage(valid.id, "worker-1", stage=TaskStage.CHUNKING)
    _backdate_lease(conn, valid.id, seconds_ago=10)
    assert repo.requeue_stale_running() == 0
    assert repo.get(valid.id).state is TaskStatus.RUNNING


def test_running_without_lease_treated_as_stale(task_repo):
    """执行中但无租约的任务视为失效（无人持有执行许可）"""
    repo, conn, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(claimed.id, "worker-1", stage=TaskStage.NORMALIZING)
    conn.execute(
        "UPDATE tasks SET lease_owner = NULL, lease_expires_at = NULL,"
        " heartbeat_at = NULL WHERE id = ?",
        (claimed.id,),
    )

    assert repo.requeue_stale_running() == 1
    assert repo.get(claimed.id).state is TaskStatus.QUEUED


def test_stale_waiting_external_requeued_for_poll_resume(task_repo):
    """等待外部结果的任务租约失效后重排回排队：阶段保留供续跑轮询"""
    repo, conn, _ = task_repo
    task = _claimed_task(repo)
    repo.transition(task.id, TaskStatus.WAITING_EXTERNAL, stage=TaskStage.POLLING_CLOUD)
    _backdate_lease(conn, task.id, seconds_ago=31)

    assert repo.recover_interrupted_tasks()["stale_running_requeued"] == 1

    loaded = repo.get(task.id)
    assert loaded.state is TaskStatus.QUEUED
    assert loaded.stage is TaskStage.POLLING_CLOUD
    assert loaded.lease_owner is None
    assert loaded.lease_expires_at is None

    # 幂等：重复恢复不再产生变化
    assert repo.recover_interrupted_tasks()["stale_running_requeued"] == 0


def test_waiting_external_within_grace_or_valid_lease_kept(task_repo):
    """等待外部结果的任务租约未失效时不被重排"""
    repo, _, _ = task_repo
    task = _claimed_task(repo)
    repo.transition(task.id, TaskStatus.WAITING_EXTERNAL, stage=TaskStage.POLLING_CLOUD)

    assert repo.requeue_stale_running() == 0
    assert repo.get(task.id).state is TaskStatus.WAITING_EXTERNAL


def test_stale_cancel_request_finished_directly(task_repo):
    """等待取消但租约已失效：恢复流程直接收尾为取消终态"""
    repo, conn, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(claimed.id, "worker-1", stage=TaskStage.EMBEDDING)
    repo.request_cancel(claimed.id)
    _backdate_lease(conn, claimed.id, seconds_ago=31)

    assert repo.finish_stale_cancel_requests() == 1

    finished = repo.get(claimed.id)
    assert finished.state is TaskStatus.CANCELLED
    assert finished.finished_at is not None
    assert finished.lease_owner is None
    assert finished.cancel_requested_at is not None


def test_cancel_request_with_valid_lease_kept(task_repo):
    """等待取消但租约仍有效：等待 Worker 到检查点收尾，不由恢复接管"""
    repo, _, _ = task_repo
    claimed = _claimed_task(repo)
    repo.request_cancel(claimed.id)

    assert repo.finish_stale_cancel_requests() == 0
    assert repo.get(claimed.id).state is TaskStatus.CANCEL_REQUESTED


def test_cancel_request_without_lease_finished(task_repo):
    """等待重试的任务被取消后无租约持有者：由恢复流程直接收尾"""
    repo, _, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(claimed.id, "worker-1", stage=TaskStage.VALIDATING)
    # 安排重试释放租约后请求取消：该任务已无持有者可到达检查点
    repo.schedule_retry(claimed.id, "worker-1", error_code="X")
    repo.request_cancel(claimed.id)

    assert repo.finish_stale_cancel_requests() == 1
    assert repo.get(claimed.id).state is TaskStatus.CANCELLED


def test_recovery_orchestrates_all_states(task_repo):
    """全状态恢复编排：失效执行/取消与到期重试被处理，其余状态保持"""
    repo, conn, _ = task_repo

    # 到期重试
    due = _claimed_task(repo)
    repo.update_stage(due.id, "worker-1", stage=TaskStage.VALIDATING)
    repo.schedule_retry(due.id, "worker-1", error_code="X")
    conn.execute(
        "UPDATE tasks SET next_retry_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (due.id,),
    )

    # 失效执行
    stale_running = _claimed_task(repo, priority=10)
    repo.update_stage(stale_running.id, "worker-1", stage=TaskStage.CHUNKING)
    _backdate_lease(conn, stale_running.id, seconds_ago=31)

    # 失效取消
    stale_cancel = _claimed_task(repo, priority=20)
    repo.request_cancel(stale_cancel.id)
    _backdate_lease(conn, stale_cancel.id, seconds_ago=31)

    # 应保持的状态
    valid_running = _claimed_task(repo, priority=30)
    waiting_external = _claimed_task(repo, priority=40)
    repo.transition(waiting_external.id, TaskStatus.WAITING_EXTERNAL)
    waiting_user = _claimed_task(repo, priority=50)
    repo.transition(waiting_user.id, TaskStatus.WAITING_USER)
    plain_queued = repo.create("import", priority=60)
    terminal = _claimed_task(repo, priority=70)
    repo.transition(terminal.id, TaskStatus.SUCCEEDED, stage=TaskStage.COMPLETED)

    counts = repo.recover_interrupted_tasks()
    assert counts == {
        "retries_promoted": 1,
        "stale_running_requeued": 1,
        "stale_cancels_finished": 1,
    }

    assert repo.get(due.id).state is TaskStatus.QUEUED
    assert repo.get(stale_running.id).state is TaskStatus.QUEUED
    assert repo.get(stale_cancel.id).state is TaskStatus.CANCELLED
    assert repo.get(valid_running.id).state is TaskStatus.RUNNING
    assert repo.get(waiting_external.id).state is TaskStatus.WAITING_EXTERNAL
    assert repo.get(waiting_user.id).state is TaskStatus.WAITING_USER
    assert repo.get(plain_queued.id).state is TaskStatus.QUEUED
    assert repo.get(terminal.id).state is TaskStatus.SUCCEEDED

    # 幂等：重复恢复不再产生变化
    assert repo.recover_interrupted_tasks() == {
        "retries_promoted": 0,
        "stale_running_requeued": 0,
        "stale_cancels_finished": 0,
    }


def test_recovery_then_reclaim_resumes_from_checkpoint(task_repo):
    """崩溃恢复闭环：重排回排队后重新领取，从断点继续且计数正确累加"""
    repo, conn, _ = task_repo
    claimed = _claimed_task(repo)
    repo.update_stage(
        claimed.id, "worker-1",
        stage=TaskStage.PARSING_LOCAL,
        progress=0.4,
        checkpoint_json='{"cursor": 7}',
    )
    _backdate_lease(conn, claimed.id, seconds_ago=31)
    repo.recover_interrupted_tasks()

    resumed = repo.claim_next("worker-2")
    assert resumed.id == claimed.id
    assert resumed.attempt_count == 2
    assert resumed.stage_attempt == 1
    assert resumed.total_attempt_count == 0
    assert resumed.checkpoint_json == '{"cursor": 7}'

    refreshed = repo.update_stage(
        resumed.id, "worker-2",
        stage=TaskStage.PARSING_LOCAL,
        progress=0.6,
        checkpoint_json='{"cursor": 12}',
    )
    assert refreshed.stage_attempt == 1
    assert refreshed.progress == 0.6
    assert refreshed.checkpoint_json == '{"cursor": 12}'
