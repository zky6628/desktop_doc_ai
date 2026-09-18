# -*- coding: utf-8 -*-
"""任务租约与 Worker 领取/心跳/阶段写入测试：
租约感知容量、并发领取原子性、守卫写入与计数语义"""
import threading

import pytest

from app.domain.entities import Task, TaskStage, TaskStatus
from app.domain.errors import (
    RepositoryError,
    TaskLeaseLostError,
    TaskStateConflictError,
)
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import SQLiteTaskRepository

from .schema_helpers import fresh_db, insert_task

# 过去/未来固定时刻（UTC ISO-8601），用于构造过期与有效租约
_PAST_TIME = "2020-01-01T00:00:00+00:00"


@pytest.fixture()
def task_repo(tmp_path):
    """应用全部迁移的临时库 + 任务仓储与库路径"""
    db_path, _ = fresh_db(tmp_path, name="task_lease.db")
    conn = connect(db_path)
    yield SQLiteTaskRepository(conn), conn, db_path
    conn.close()


def _backdate_lease(conn, task_id: str, expires_at: str = _PAST_TIME) -> None:
    """把任务租约到期时间改写为指定时刻（测试构造过期/未来租约）"""
    conn.execute(
        "UPDATE tasks SET lease_expires_at = ? WHERE id = ?", (expires_at, task_id)
    )


def _release_lease(conn, task_id: str) -> None:
    """清空任务租约（模拟取消清理后的租约释放）"""
    conn.execute(
        "UPDATE tasks SET lease_owner = NULL, lease_expires_at = NULL,"
        " heartbeat_at = NULL WHERE id = ?",
        (task_id,),
    )


def test_claim_sets_lease_and_execution_counts(task_repo):
    """领取成功：置 running 与租约字段，执行计数 +1，并记录审计事件"""
    repo, _, _ = task_repo
    task = repo.create("import")

    claimed = repo.claim_next("worker-1")
    assert claimed is not None
    assert claimed.id == task.id
    assert claimed.state is TaskStatus.RUNNING
    assert claimed.lease_owner == "worker-1"
    assert claimed.lease_expires_at is not None
    assert claimed.heartbeat_at is not None
    assert claimed.started_at is not None
    assert claimed.attempt_count == 1
    assert claimed.total_attempt_count == 1
    # 阶段尚未开始：stage_attempt 由阶段写入置 1
    assert claimed.stage_attempt == 0

    events = repo.list_events(task.id)
    assert [(event.event_type, event.state) for event in events] == [
        ("created", TaskStatus.QUEUED),
        ("state_changed", TaskStatus.RUNNING),
    ]
    assert events[-1].worker == "worker-1"
    assert events[-1].attempt_count == 1


def test_claim_orders_by_priority_then_fifo(task_repo):
    """领取顺序：priority 降序优先，同优先级按创建时间先进先出"""
    repo, conn, _ = task_repo
    low = insert_task(conn, priority=0, created_at="2026-01-01T00:00:00+00:00")
    first = insert_task(conn, priority=5, created_at="2026-01-02T00:00:00+00:00")
    second = insert_task(conn, priority=5, created_at="2026-01-03T00:00:00+00:00")

    assert repo.claim_next("worker-1").id == first
    assert repo.claim_next("worker-1").id == second
    assert repo.claim_next("worker-1").id == low


def test_claim_returns_none_when_queue_empty(task_repo):
    """无排队任务时领取返回 None"""
    repo, _, _ = task_repo
    assert repo.claim_next("worker-1") is None


def test_claim_returns_none_when_execution_slots_full(task_repo):
    """三个有效执行租约占满后，第四个任务保持 queued 不可领取"""
    repo, _, _ = task_repo
    tasks = [repo.create("import") for _ in range(4)]
    for _ in range(3):
        assert repo.claim_next("worker") is not None

    assert repo.claim_next("worker") is None
    remaining = repo.get(tasks[3].id)
    assert remaining.state is TaskStatus.QUEUED


def test_cancel_requested_with_lease_blocks_claim(task_repo):
    """等待取消但租约未释放的任务仍占执行许可，释放后让出名额"""
    repo, conn, _ = task_repo
    tasks = [repo.create("import") for _ in range(4)]
    claimed = [repo.claim_next("worker") for _ in range(3)]

    # 其中一个进入等待取消：租约未释放，仍占执行许可
    repo.transition(claimed[0].id, TaskStatus.CANCEL_REQUESTED)
    assert repo.claim_next("worker") is None

    # 释放租约后名额让出（取消清理完成后的形态）
    _release_lease(conn, claimed[0].id)
    fourth = repo.claim_next("worker")
    assert fourth is not None
    assert fourth.id == tasks[3].id


def test_expired_lease_does_not_block_claim(task_repo):
    """过期租约（Worker 失联）不计入执行许可，新任务可继续领取"""
    repo, conn, _ = task_repo
    tasks = [repo.create("import") for _ in range(4)]
    claimed = [repo.claim_next("worker") for _ in range(3)]
    assert repo.claim_next("worker") is None

    _backdate_lease(conn, claimed[0].id)
    fourth = repo.claim_next("worker")
    assert fourth is not None
    assert fourth.id == tasks[3].id


def test_waiting_external_does_not_occupy_execution_slot(task_repo):
    """等待外部解析结果的任务不占执行许可：两个 running 加一个等待
    仍可领取第三个执行任务"""
    repo, _, _ = task_repo
    tasks = [repo.create("import") for _ in range(3)]

    first = repo.claim_next("worker")
    second = repo.claim_next("worker")
    repo.transition(second.id, TaskStatus.WAITING_EXTERNAL)

    third = repo.claim_next("worker")
    assert third is not None
    assert {first.id, second.id, third.id} == {task.id for task in tasks}


def test_concurrent_claim_has_single_winner(task_repo):
    """两个 Worker 并发领取同一任务：恰好一人得到，计数只加一次"""
    repo, _, db_path = task_repo
    repo.create("import")

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def _claim(worker_id: str) -> None:
        worker_conn = connect(db_path)
        try:
            barrier.wait()
            results[worker_id] = SQLiteTaskRepository(worker_conn).claim_next(worker_id)
        except Exception as exc:  # noqa: BLE001 - 测试断言需跨线程传播任意异常
            results[worker_id] = exc
        finally:
            worker_conn.close()

    threads = [
        threading.Thread(target=_claim, args=(name,)) for name in ("w-a", "w-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    winners = [name for name, value in results.items() if isinstance(value, Task)]
    losers = [name for name, value in results.items() if value is None]
    assert len(winners) == 1
    assert len(losers) == 1


def test_heartbeat_renews_lease(task_repo):
    """心跳续约：延长到期时间并刷新心跳时刻"""
    repo, _, _ = task_repo
    task = repo.create("import")
    claimed = repo.claim_next("worker-1")

    renewed = repo.heartbeat(task.id, "worker-1")
    assert renewed.lease_expires_at >= claimed.lease_expires_at
    assert renewed.heartbeat_at is not None
    # 续约后租约保持有效：后续守卫写入正常执行
    still_valid = repo.update_stage(task.id, "worker-1", stage=TaskStage.VALIDATING)
    assert still_valid.stage is TaskStage.VALIDATING


def test_heartbeat_with_wrong_worker_rejected(task_repo):
    """非持有者心跳被拒绝"""
    repo, _, _ = task_repo
    task = repo.create("import")
    repo.claim_next("worker-1")

    with pytest.raises(TaskLeaseLostError):
        repo.heartbeat(task.id, "worker-2")


def test_heartbeat_after_lease_expired_rejected(task_repo):
    """租约过期后心跳被拒绝（失约 Worker 必须停止提交）"""
    repo, conn, _ = task_repo
    task = repo.create("import")
    repo.claim_next("worker-1")
    _backdate_lease(conn, task.id)

    with pytest.raises(TaskLeaseLostError):
        repo.heartbeat(task.id, "worker-1")


def test_update_stage_writes_and_counts_first_stage_attempt(task_repo):
    """阶段写入：新阶段 stage_attempt 置 1，同阶段刷新保持计数，
    progress/checkpoint 随写随存"""
    repo, _, _ = task_repo
    task = repo.create("import")
    repo.claim_next("worker-1")

    started = repo.update_stage(
        task.id,
        "worker-1",
        stage=TaskStage.VALIDATING,
        progress=0.1,
        checkpoint_json='{"last_stage": "validating"}',
    )
    assert started.stage is TaskStage.VALIDATING
    assert started.stage_attempt == 1
    assert started.progress == 0.1
    assert started.checkpoint_json == '{"last_stage": "validating"}'

    # 同阶段进度刷新：执行计数不变
    refreshed = repo.update_stage(task.id, "worker-1", stage=TaskStage.VALIDATING)
    assert refreshed.stage_attempt == 1

    # 阶段成功归零，随后进入新阶段重新计 1
    completed = repo.complete_stage(task.id, "worker-1", stage=TaskStage.VALIDATING)
    assert completed.stage_attempt == 0

    next_stage = repo.update_stage(task.id, "worker-1", stage=TaskStage.CHUNKING)
    assert next_stage.stage is TaskStage.CHUNKING
    assert next_stage.stage_attempt == 1


def test_update_stage_requires_valid_lease(task_repo):
    """阶段写入守卫：未领取、错手与过期租约均被拒绝"""
    repo, conn, _ = task_repo
    # 高优先级任务保证被领取，另一任务保持排队
    unclaimed = repo.create("import")
    claimed = repo.create("import", priority=10)
    assert repo.claim_next("worker-1").id == claimed.id

    with pytest.raises(TaskLeaseLostError):
        repo.update_stage(unclaimed.id, "worker-1", stage=TaskStage.VALIDATING)
    with pytest.raises(TaskLeaseLostError):
        repo.update_stage(claimed.id, "worker-2", stage=TaskStage.VALIDATING)

    _backdate_lease(conn, claimed.id)
    with pytest.raises(TaskLeaseLostError):
        repo.update_stage(claimed.id, "worker-1", stage=TaskStage.VALIDATING)


def test_complete_stage_mismatch_rejected(task_repo):
    """阶段完成与当前阶段不一致时拒绝"""
    repo, _, _ = task_repo
    task = repo.create("import")
    repo.claim_next("worker-1")
    repo.update_stage(task.id, "worker-1", stage=TaskStage.VALIDATING)

    with pytest.raises(TaskStateConflictError):
        repo.complete_stage(task.id, "worker-1", stage=TaskStage.CHUNKING)


def test_busy_retry_on_claim_does_not_inflate_attempt(task_repo):
    """busy 短重试不改变业务执行计数：持锁导致重试耗尽后，
    重放领取成功且 attempt 只计一次"""
    repo, _, db_path = task_repo
    repo.create("import")

    blocker = connect(db_path)
    victim_conn = connect(db_path, busy_timeout_ms=50)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("CREATE TABLE blocker_keepalive (id INTEGER)")

        with pytest.raises(RepositoryError):
            SQLiteTaskRepository(victim_conn).claim_next("worker-1")

        blocker.execute("COMMIT")
        claimed = SQLiteTaskRepository(victim_conn).claim_next("worker-1")
        assert claimed is not None
        assert claimed.attempt_count == 1
        assert claimed.total_attempt_count == 1
    finally:
        victim_conn.close()
        blocker.close()


def test_claim_filters_by_task_type(task_repo):
    """按任务类型领取：只取匹配类型的排队任务"""
    repo, _, _ = task_repo
    repo.create("delete")
    import_task = repo.create("import")

    claimed = repo.claim_next("worker-1", task_type="import")
    assert claimed.id == import_task.id

    # 指定类型无排队任务时返回 None（即使其他类型仍有排队）
    assert repo.claim_next("worker-1", task_type="health_check") is None
