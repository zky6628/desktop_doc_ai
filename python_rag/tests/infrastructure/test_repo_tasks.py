# -*- coding: utf-8 -*-
"""任务仓储集成测试：原子容量、幂等创建、状态机迁移与审计事件"""
import threading

import pytest

from app.domain.entities import Task, TaskStage, TaskStatus
from app.domain.errors import (
    EntityNotFoundError,
    RepositoryError,
    TaskQueueFullError,
    TaskStateConflictError,
)
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteKnowledgeBaseRepository,
    SQLiteTaskRepository,
)

from .schema_helpers import fresh_db, insert_task


@pytest.fixture()
def repos(tmp_path):
    """应用全部迁移的临时库 + 任务仓储（含上游链路仓储）与库路径"""
    db_path, _ = fresh_db(tmp_path, name="repo_tasks.db")
    conn = connect(db_path)
    repositories = {
        "tasks": SQLiteTaskRepository(conn),
        "kb": SQLiteKnowledgeBaseRepository(conn),
        "documents": SQLiteDocumentRepository(conn),
        "versions": SQLiteDocumentVersionRepository(conn),
    }
    yield repositories, conn, db_path
    conn.close()


def _count(conn, table="tasks") -> int:
    """统计表行数（测试辅助）"""
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_create_defaults_to_queued_and_writes_event(repos):
    """创建任务默认 queued：计数与租约字段全零/空，并写入 created 事件"""
    repositories, _, _ = repos
    task = repositories["tasks"].create("import", input_json='{"file": "报告.pdf"}')

    assert task.state is TaskStatus.QUEUED
    assert task.stage is None
    assert task.progress == 0.0
    assert task.priority == 0
    assert task.retry_count == 0
    assert task.max_retries == 3
    assert (task.attempt_count, task.stage_attempt, task.total_attempt_count) == (0, 0, 0)
    assert task.idempotency_key is None
    assert task.lease_owner is None
    assert task.started_at is None
    assert task.finished_at is None
    assert task.cancel_requested_at is None
    assert task.input_json == '{"file": "报告.pdf"}'
    assert task.created_at is not None

    events = repositories["tasks"].list_events(task.id)
    assert len(events) == 1
    assert events[0].event_type == "created"
    assert events[0].state is TaskStatus.QUEUED
    assert events[0].stage is None
    assert events[0].attempt_count == 0


def test_create_with_reference_chain(repos):
    """任务携带知识库/文档/版本/索引引用创建并完整读回"""
    repositories, _, _ = repos
    kb = repositories["kb"].create(name="kb")
    doc = repositories["documents"].create(kb.id, "报告.pdf", "a" * 64)
    version = repositories["versions"].create(doc.id, "uploads/报告.pdf", "a" * 64)

    task = repositories["tasks"].create(
        "import",
        knowledge_base_id=kb.id,
        document_id=doc.id,
        document_version_id=version.id,
        priority=5,
    )
    loaded = repositories["tasks"].get(task.id)
    assert loaded == task
    assert loaded.knowledge_base_id == kb.id
    assert loaded.document_id == doc.id
    assert loaded.document_version_id == version.id
    assert loaded.priority == 5


def test_idempotency_key_replay_returns_same_task(repos):
    """同幂等键重放返回同一任务，不产生新任务或重复事件"""
    repositories, conn, _ = repos
    first = repositories["tasks"].create("import", idempotency_key="idem-1")
    replay = repositories["tasks"].create("import", idempotency_key="idem-1")

    assert replay.id == first.id
    assert replay.created_at == first.created_at
    assert _count(conn) == 1
    assert len(repositories["tasks"].list_events(first.id)) == 1

    # 不同键产生不同任务
    other = repositories["tasks"].create("import", idempotency_key="idem-2")
    assert other.id != first.id


def test_fourth_task_stays_queued_with_three_running(repos):
    """已有三个 running 时第四个任务仍可创建并保持 queued 排队"""
    repositories, _, _ = repos
    repo = repositories["tasks"]
    tasks = [repo.create("import") for _ in range(4)]
    for task in tasks[:3]:
        repo.transition(task.id, TaskStatus.RUNNING, stage=TaskStage.VALIDATING)

    states = [repo.get(task.id).state for task in tasks]
    assert states == [
        TaskStatus.RUNNING, TaskStatus.RUNNING, TaskStatus.RUNNING, TaskStatus.QUEUED,
    ]


def test_pending_capacity_full_rejects_creation(repos):
    """pending 合计达到 50 后新提交返回队列已满错误，且不产生残留"""
    repositories, conn, _ = repos
    repo = repositories["tasks"]
    for _ in range(50):
        repo.create("import")

    with pytest.raises(TaskQueueFullError):
        repo.create("import")

    assert _count(conn) == 50
    assert _count(conn, "task_events") == 50


def test_terminal_tasks_free_capacity(repos):
    """终态任务不计入容量：取消与失败释放名额后可继续创建"""
    repositories, conn, _ = repos
    repo = repositories["tasks"]
    tasks = [repo.create("import") for _ in range(50)]

    # 取消 5 个（queued -> cancelled 合法），再走 running -> failed 释放 5 个
    for task in tasks[:5]:
        repo.transition(task.id, TaskStatus.CANCELLED)
    for task in tasks[5:10]:
        repo.transition(task.id, TaskStatus.RUNNING)
        repo.transition(task.id, TaskStatus.FAILED)

    for _ in range(10):
        repo.create("import")

    assert _count(conn) == 60
    with pytest.raises(TaskQueueFullError):
        repo.create("import")


def test_non_terminal_capacity_cap_53(repos):
    """非终态合计 53 封顶：pending 未超限时非终态合计越界同样被拒绝"""
    repositories, _, _ = repos
    repo = repositories["tasks"]

    # 构造 4 running + 45 pending：先建 49 个再迁移 4 个进入 running
    tasks = [repo.create("import") for _ in range(49)]
    for task in tasks[:4]:
        repo.transition(task.id, TaskStatus.RUNNING)

    # 回填 pending 至 49：非终态合计 4+49=53 恰好触顶
    for _ in range(4):
        repo.create("import")

    # 再创建一个：pending 50 未超限，但非终态合计 54 越界
    with pytest.raises(TaskQueueFullError):
        repo.create("import")

    # 一个 running 进入终态后容量释放
    repo.transition(tasks[0].id, TaskStatus.FAILED)
    created = repo.create("import")
    assert created.state is TaskStatus.QUEUED


def test_concurrent_create_respects_capacity(repos):
    """两个独立连接并发提交最后一个名额：恰好一成一败，容量不超限"""
    repositories, conn, db_path = repos
    for _ in range(49):
        repositories["tasks"].create("import")

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def _submit(key: str) -> None:
        # 每个线程持有独立连接（SQLite 连接不可跨线程使用）
        worker_conn = connect(db_path)
        try:
            barrier.wait()
            results[key] = SQLiteTaskRepository(worker_conn).create(
                "import", idempotency_key=key
            )
        except TaskQueueFullError as exc:
            results[key] = exc
        except Exception as exc:  # noqa: BLE001 - 测试断言需跨线程传播任意异常
            results[key] = exc
        finally:
            worker_conn.close()

    threads = [threading.Thread(target=_submit, args=(key,)) for key in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    succeeded = [k for k, v in results.items() if isinstance(v, Task)]
    rejected = [k for k, v in results.items() if isinstance(v, TaskQueueFullError)]
    assert len(succeeded) == 1
    assert len(rejected) == 1
    assert _count(conn) == 50


def test_busy_short_retry_preserves_capacity_counts(repos):
    """busy 短重试不污染计数：持锁期间提交耗尽重试并回滚，
    锁释放后读到最新容量并正确拒绝"""
    repositories, conn, db_path = repos
    for _ in range(49):
        repositories["tasks"].create("import")

    blocker = connect(db_path)
    victim_conn = connect(db_path, busy_timeout_ms=50)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        # 持锁写入第 50 个任务（未提交）：短重试期间对其他写者不可见
        insert_task(blocker, idempotency_key="blocked-50")

        with pytest.raises(RepositoryError):
            SQLiteTaskRepository(victim_conn).create(
                "import", idempotency_key="victim-1"
            )
        # 重试耗尽已整体回滚：无任务、无事件残留
        assert _count(conn) == 49
        assert _count(conn, "task_events") == 49

        blocker.execute("COMMIT")
        assert _count(conn) == 50

        # 锁释放后容量读到最新事实：第 51 个提交被拒绝
        with pytest.raises(TaskQueueFullError):
            SQLiteTaskRepository(victim_conn).create(
                "import", idempotency_key="victim-2"
            )
        assert _count(conn) == 50
    finally:
        victim_conn.close()
        blocker.close()


def test_transition_happy_path_records_events_and_timestamps(repos):
    """合法迁移全链路：状态/阶段更新、事件按序追加、时间戳正确回填"""
    repositories, _, _ = repos
    repo = repositories["tasks"]
    task = repo.create("import")

    running = repo.transition(task.id, TaskStatus.RUNNING, stage=TaskStage.VALIDATING)
    assert running.started_at is not None
    waiting = repo.transition(
        task.id, TaskStatus.WAITING_EXTERNAL, stage=TaskStage.SUBMITTING_CLOUD
    )
    assert waiting.started_at == running.started_at
    # 等待外部结果的任务经 queued 回流后重新领取（无 waiting_external -> running 直达边）
    repo.transition(task.id, TaskStatus.QUEUED)
    repo.transition(task.id, TaskStatus.RUNNING, stage=TaskStage.POLLING_CLOUD)
    succeeded = repo.transition(
        task.id, TaskStatus.SUCCEEDED, stage=TaskStage.COMPLETED
    )
    assert succeeded.state is TaskStatus.SUCCEEDED
    assert succeeded.finished_at is not None
    assert succeeded.progress == 0.0  # 进度推进属 Worker 职责，迁移本身不改写

    events = repo.list_events(task.id)
    assert [(event.event_type, event.state) for event in events] == [
        ("created", TaskStatus.QUEUED),
        ("state_changed", TaskStatus.RUNNING),
        ("state_changed", TaskStatus.WAITING_EXTERNAL),
        ("state_changed", TaskStatus.QUEUED),
        ("state_changed", TaskStatus.RUNNING),
        ("state_changed", TaskStatus.SUCCEEDED),
    ]
    assert [event.stage for event in events] == [
        None, TaskStage.VALIDATING, TaskStage.SUBMITTING_CLOUD,
        TaskStage.SUBMITTING_CLOUD, TaskStage.POLLING_CLOUD, TaskStage.COMPLETED,
    ]


def test_transition_cancel_flow_timestamps(repos):
    """取消链路：进入 cancel_requested 回填请求时间，终态回填完成时间"""
    repositories, _, _ = repos
    repo = repositories["tasks"]
    task = repo.create("import")
    repo.transition(task.id, TaskStatus.RUNNING)

    requested = repo.transition(task.id, TaskStatus.CANCEL_REQUESTED)
    assert requested.cancel_requested_at is not None

    cancelled = repo.transition(task.id, TaskStatus.CANCELLED)
    assert cancelled.state is TaskStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert cancelled.cancel_requested_at == requested.cancel_requested_at


def test_illegal_transition_rejected_and_recorded(repos):
    """非法迁移抛状态冲突错误：任务保持原状，失败事件带错误码与迁移对"""
    repositories, conn, _ = repos
    repo = repositories["tasks"]
    task = repo.create("import")

    with pytest.raises(TaskStateConflictError):
        repo.transition(task.id, TaskStatus.FAILED)

    loaded = repo.get(task.id)
    assert loaded.state is TaskStatus.QUEUED

    events = repo.list_events(task.id)
    assert [event.event_type for event in events] == ["created", "transition_rejected"]
    rejected = events[-1]
    assert rejected.error_code == "TASK_STATE_CONFLICT"
    assert '"from_state": "queued"' in rejected.detail_json
    assert '"to_state": "failed"' in rejected.detail_json

    # 拒绝不破坏任务：后续合法迁移照常执行
    moved = repo.transition(task.id, TaskStatus.CANCELLED)
    assert moved.state is TaskStatus.CANCELLED
    assert _count(conn, "task_events") == 3


def test_transition_from_terminal_rejected_and_recorded(repos):
    """终态无出边：取消后再迁移被拒绝并记录失败事件"""
    repositories, _, _ = repos
    repo = repositories["tasks"]
    task = repo.create("import")
    repo.transition(task.id, TaskStatus.CANCELLED)

    with pytest.raises(TaskStateConflictError):
        repo.transition(task.id, TaskStatus.RUNNING)

    events = repo.list_events(task.id)
    assert events[-1].event_type == "transition_rejected"
    assert events[-1].state is TaskStatus.CANCELLED


def test_self_transition_rejected(repos):
    """自迁移不在迁移表中：queued -> queued 被拒绝"""
    repositories, _, _ = repos
    repo = repositories["tasks"]
    task = repo.create("import")

    with pytest.raises(TaskStateConflictError):
        repo.transition(task.id, TaskStatus.QUEUED)


def test_transition_missing_task_raises_entity_not_found(repos):
    """对不存在的任务迁移抛实体不存在错误"""
    repositories, conn, _ = repos
    with pytest.raises(EntityNotFoundError):
        repositories["tasks"].transition(
            "01900000-0000-7000-8000-000000000000", TaskStatus.RUNNING
        )
    assert _count(conn, "task_events") == 0


def test_capacity_counts_only_pending_and_running_states(repos):
    """容量口径验证：构造 waiting_external/retry_waiting 混合队列，
    pending 合计精确计算后拒绝超额提交"""
    repositories, conn, _ = repos
    repo = repositories["tasks"]
    tasks = [repo.create("import") for _ in range(48)]

    # 1 个进入 waiting_external（pending 口径）、1 个进入 running 后转
    # retry_waiting（pending 口径）
    repo.transition(tasks[0].id, TaskStatus.RUNNING)
    repo.transition(tasks[0].id, TaskStatus.WAITING_EXTERNAL)
    repo.transition(tasks[1].id, TaskStatus.RUNNING)
    repo.transition(tasks[1].id, TaskStatus.RETRY_WAITING)

    # pending = 46 queued + 1 waiting_external + 1 retry_waiting = 48，
    # 再创建 2 个后达到 50 上限
    repo.create("import")
    repo.create("import")
    assert _count(conn) == 50
    with pytest.raises(TaskQueueFullError):
        repo.create("import")
