# -*- coding: utf-8 -*-
"""任务仓储的 SQLite 实现（原子容量检查、幂等创建与状态机迁移）"""
import json
from datetime import datetime, timedelta, timezone

from app.domain import task_state
from app.domain.clock import utc_now_iso
from app.domain.entities import Task, TaskEvent, TaskStage, TaskStatus
from app.domain.errors import (
    EntityNotFoundError,
    TaskLeaseLostError,
    TaskQueueFullError,
    TaskStateConflictError,
)
from app.domain.ids import uuid7
from app.domain.ports import TaskRepository as TaskRepositoryPort

from ..transactions import run_in_transaction

# 审计事件类型：创建、状态迁移成功、状态迁移被拒
_EVENT_CREATED = "created"
_EVENT_STATE_CHANGED = "state_changed"
_EVENT_TRANSITION_REJECTED = "transition_rejected"

# 非法迁移审计事件携带的错误码
_STATE_CONFLICT_CODE = "TASK_STATE_CONFLICT"

# pending 容量统计的 state 过滤子句（集合内容由领域层统一定义）
_PENDING_STATE_VALUES = tuple(sorted(state.value for state in task_state.PENDING_STATES))
_PENDING_STATE_PLACEHOLDERS = ",".join("?" * len(_PENDING_STATE_VALUES))

_TASK_COLUMNS = (
    "id, task_type, knowledge_base_id, document_id, document_version_id,"
    " index_version_id, state, stage, progress, priority, idempotency_key,"
    " retry_count, max_retries, attempt_count, stage_attempt, total_attempt_count,"
    " next_retry_at, lease_owner, lease_expires_at, heartbeat_at,"
    " cancel_requested_at, checkpoint_json, parent_task_id, retry_origin,"
    " error_code, error_message, input_json, created_at, started_at, finished_at"
)

_EVENT_COLUMNS = (
    "id, task_id, event_type, state, stage, attempt_count, worker,"
    " created_at, duration_ms, checkpoint_json, error_code, detail_json"
)


def _lease_expiry_iso(seconds: int) -> str:
    """返回当前 UTC 时间加上指定秒数后的 ISO-8601 文本（与时间戳字段同格式）"""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def _stale_lease_cutoff_iso() -> str:
    """租约失效判定阈值：当前时间减去接管宽限。

    早于该时刻过期的租约才视为持有者已失联，避免把仍在执行、
    只是两次心跳间隔偏大的 Worker 误判为中断而造成重复执行
    """
    return (
        datetime.now(timezone.utc)
        - timedelta(seconds=task_state.TAKEOVER_GRACE_SECONDS)
    ).isoformat(timespec="seconds")


def ensure_queue_capacity(conn) -> None:
    """校验任务队列容量（须在写事务内调用）

    pending 合计与非终态合计各自不得超过上限；running 的物理执行
    许可由领取逻辑做租约感知计数，创建阶段不拦截，因此已有 3 个
    running 时新任务仍可创建并保持 queued 排队。

    :param conn: 事务内的数据库连接
    :raises TaskQueueFullError: 任一口径达到上限
    """
    pending = conn.execute(
        "SELECT COUNT(*) FROM tasks"
        f" WHERE state IN ({_PENDING_STATE_PLACEHOLDERS})",
        _PENDING_STATE_VALUES,
    ).fetchone()[0]
    running = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE state = ?",
        (TaskStatus.RUNNING.value,),
    ).fetchone()[0]
    if pending + 1 > task_state.MAX_PENDING:
        raise TaskQueueFullError(
            f"任务队列已满: pending={pending}, 上限={task_state.MAX_PENDING}"
        )
    if running + pending + 1 > task_state.MAX_NON_TERMINAL:
        raise TaskQueueFullError(
            f"任务队列已满: 非终态合计={running + pending + 1},"
            f" 上限={task_state.MAX_NON_TERMINAL}"
        )


def insert_task_event(
    conn,
    *,
    task_id: str,
    event_type: str,
    state: TaskStatus,
    stage: TaskStage | None,
    attempt_count: int,
    worker: str | None,
    created_at: str,
    duration_ms: int | None = None,
    checkpoint_json: str | None = None,
    error_code: str | None = None,
    detail_json: str | None = None,
) -> None:
    """追加一条任务审计事件（须在事务内调用）

    :param conn: 数据库连接
    :param task_id: 所属任务 ID
    :param event_type: 事件类型
    :param state: 事件时刻的任务状态
    :param stage: 事件时刻的处理阶段（可空）
    :param attempt_count: 事件时刻的执行计数
    :param worker: 事件时刻的租约持有者（可空）
    :param created_at: 事件时间（UTC ISO-8601）
    :param duration_ms: 耗时毫秒（可空）
    :param checkpoint_json: 检查点快照（可空）
    :param error_code: 错误码（可空）
    :param detail_json: 脱敏详情（可空）
    """
    conn.execute(
        "INSERT INTO task_events"
        " (id, task_id, event_type, state, stage, attempt_count, worker,"
        "  created_at, duration_ms, checkpoint_json, error_code, detail_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid7(), task_id, event_type, state.value,
            stage.value if stage is not None else None,
            attempt_count, worker, created_at, duration_ms,
            checkpoint_json, error_code, detail_json,
        ),
    )


class SQLiteTaskRepository(TaskRepositoryPort):
    """tasks / task_events 表的仓储实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def create(
        self,
        task_type: str,
        *,
        knowledge_base_id: str | None = None,
        document_id: str | None = None,
        document_version_id: str | None = None,
        index_version_id: str | None = None,
        priority: int = 0,
        idempotency_key: str | None = None,
        input_json: str | None = None,
        max_retries: int = 3,
        parent_task_id: str | None = None,
        retry_origin: str | None = None,
    ) -> Task:
        def _create(conn) -> Task:
            now = utc_now_iso()
            # 幂等重放：同键命中既有任务直接返回，保证重放不产生新任务
            if idempotency_key is not None:
                existing = conn.execute(
                    f"SELECT {_TASK_COLUMNS} FROM tasks WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._to_entity(existing)

            # 容量检查与插入同在一个 BEGIN IMMEDIATE 事务内，写者被
            # 串行化后读到的计数即为提交时刻事实，杜绝并发超限
            ensure_queue_capacity(conn)

            task_id = uuid7()
            conn.execute(
                "INSERT INTO tasks"
                " (id, task_type, knowledge_base_id, document_id, document_version_id,"
                "  index_version_id, state, stage, progress, priority, idempotency_key,"
                "  retry_count, max_retries, attempt_count, stage_attempt,"
                "  total_attempt_count, next_retry_at, lease_owner, lease_expires_at,"
                "  heartbeat_at, cancel_requested_at, checkpoint_json, parent_task_id,"
                "  retry_origin, error_code, error_message, input_json,"
                "  created_at, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, 0, ?, 0, 0, 0,"
                "  NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL, ?, ?,"
                "  NULL, NULL)",
                (
                    task_id, task_type, knowledge_base_id, document_id,
                    document_version_id, index_version_id, TaskStatus.QUEUED.value,
                    priority, idempotency_key, max_retries, parent_task_id,
                    retry_origin, input_json, now,
                ),
            )
            self._insert_event(
                conn,
                task_id=task_id,
                event_type=_EVENT_CREATED,
                state=TaskStatus.QUEUED,
                stage=None,
                attempt_count=0,
                worker=None,
                created_at=now,
            )
            row = self._get_row(conn, task_id)
            return self._to_entity(row)

        return run_in_transaction(self._conn, _create, f"创建任务 {task_type}")

    def get(self, task_id: str) -> Task | None:
        row = self._get_row(self._conn, task_id)
        return self._to_entity(row) if row is not None else None

    def transition(
        self,
        task_id: str,
        target_state: TaskStatus,
        stage: TaskStage | None = None,
    ) -> Task:
        current = self.get(task_id)
        if current is None:
            raise EntityNotFoundError(f"任务不存在: {task_id}")

        try:
            task_state.require_valid_transition(current.state, target_state)
        except TaskStateConflictError:
            # 拒绝记录必须先于错误抛出持久化：通用事务执行器遇到异常
            # 会整体回滚，因此失败事件在独立小事务中落库后再重抛
            self._record_rejected_transition(current, target_state)
            raise

        def _apply(conn) -> Task:
            # 写锁内重读并复验：BEGIN IMMEDIATE 串行化写者，
            # 校验通过到写入之间不存在并发迁移插入窗口
            row = self._get_row(conn, task_id)
            if row is None:
                raise EntityNotFoundError(f"任务不存在: {task_id}")
            task = self._to_entity(row)
            task_state.require_valid_transition(task.state, target_state)

            now = utc_now_iso()
            started_at = task.started_at
            if target_state is TaskStatus.RUNNING and started_at is None:
                started_at = now
            finished_at = task.finished_at
            if target_state in task_state.TERMINAL_STATES:
                finished_at = now
            cancel_requested_at = task.cancel_requested_at
            if target_state is TaskStatus.CANCEL_REQUESTED and cancel_requested_at is None:
                cancel_requested_at = now
            new_stage = stage if stage is not None else task.stage

            conn.execute(
                "UPDATE tasks SET state = ?, stage = ?, started_at = ?,"
                " finished_at = ?, cancel_requested_at = ? WHERE id = ?",
                (
                    target_state.value,
                    new_stage.value if new_stage is not None else None,
                    started_at, finished_at, cancel_requested_at, task_id,
                ),
            )
            self._insert_event(
                conn,
                task_id=task_id,
                event_type=_EVENT_STATE_CHANGED,
                state=target_state,
                stage=new_stage,
                attempt_count=task.attempt_count,
                worker=task.lease_owner,
                created_at=now,
                checkpoint_json=task.checkpoint_json,
            )
            return self._to_entity(self._get_row(conn, task_id))

        return run_in_transaction(
            self._conn, _apply, f"迁移任务状态 {task_id} -> {target_state.value}"
        )

    def list_events(self, task_id: str) -> list[TaskEvent]:
        # created_at 为秒精度，无法区分同一秒内的多条事件；
        # rowid 随插入单调递增，按其排序即真实写入顺序
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM task_events"
            " WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        ).fetchall()
        return [self._event_to_entity(row) for row in rows]

    def append_event(
        self, task_id: str, event_type: str, *, detail_json: str | None = None
    ) -> TaskEvent:
        """追加一条审计事件（方法契约见领域 Port 定义）"""

        def _append(conn) -> TaskEvent:
            row = self._get_row(conn, task_id)
            if row is None:
                raise EntityNotFoundError(f"任务不存在: {task_id}")
            task = self._to_entity(row)
            created_at = utc_now_iso()
            self._insert_event(
                conn,
                task_id=task_id,
                event_type=event_type,
                state=task.state,
                stage=task.stage,
                attempt_count=task.attempt_count,
                worker=task.lease_owner,
                created_at=created_at,
                detail_json=detail_json,
            )
            # 事件主键由共享写入生成，按同秒内最后写入的行取回
            event_row = conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM task_events"
                " WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return self._event_to_entity(event_row)

        return run_in_transaction(
            self._conn, _append, f"记录任务 {task_id} 的 {event_type} 事件"
        )

    def count_non_terminal_by_document_version(self, document_version_id: str) -> int:
        """统计引用该文档版本的非终态任务数（方法契约见领域 Port 定义）"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM tasks"
            " WHERE document_version_id = ?"
            f" AND state IN ({_PENDING_STATE_PLACEHOLDERS})",
            (document_version_id, *_PENDING_STATE_VALUES),
        ).fetchone()
        return row[0]

    def count_non_terminal_by_document(self, document_id: str) -> int:
        """统计引用该文档的非终态任务数（方法契约见领域 Port 定义）"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM tasks"
            " WHERE document_id = ?"
            f" AND state IN ({_PENDING_STATE_PLACEHOLDERS})",
            (document_id, *_PENDING_STATE_VALUES),
        ).fetchone()
        return row[0]

    def list_by_document(self, document_id: str, limit: int = 5) -> list[Task]:
        """按创建时间倒序列出引用该文档的最近任务（方法契约见领域 Port 定义）"""
        rows = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks"
            " WHERE document_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (document_id, limit),
        ).fetchall()
        return [self._to_entity(row) for row in rows]

    def list_tasks(
        self,
        *,
        states=None,
        task_type: str | None = None,
        knowledge_base_id: str | None = None,
        document_id: str | None = None,
        limit: int = 50,
        after_created_at: str | None = None,
        after_id: str | None = None,
    ) -> list[Task]:
        """按筛选条件列出任务（方法契约见领域 Port 定义）"""
        sql = f"SELECT {_TASK_COLUMNS} FROM tasks WHERE 1=1"
        params: list[str | int] = []
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            params.extend(state.value for state in states)
        if task_type is not None:
            sql += " AND task_type = ?"
            params.append(task_type)
        if knowledge_base_id is not None:
            sql += " AND knowledge_base_id = ?"
            params.append(knowledge_base_id)
        if document_id is not None:
            sql += " AND document_id = ?"
            params.append(document_id)
        if after_created_at is not None and after_id is not None:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            params.extend([after_created_at, after_created_at, after_id])
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._to_entity(row) for row in rows]

    def queue_position(self, task_id: str) -> int | None:
        """queued 任务的队列位次（priority 降序、created_at/id 升序）

        非排队状态返回 None；任务不存在返回 None
        """
        task = self._conn.execute(
            "SELECT state, priority, created_at, id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task is None or task[0] != TaskStatus.QUEUED.value:
            return None
        ahead = self._conn.execute(
            "SELECT COUNT(*) FROM tasks"
            " WHERE state = ?"
            " AND (priority > ?"
            "      OR (priority = ? AND (created_at < ?"
            "          OR (created_at = ? AND id < ?))))",
            (
                TaskStatus.QUEUED.value,
                task[1], task[1], task[2], task[2], task[3],
            ),
        ).fetchone()[0]
        return ahead + 1

    def claim_next(
        self, worker_id: str, task_type: str | None = None
    ) -> Task | None:
        def _claim(conn) -> Task | None:
            now = utc_now_iso()
            # 物理执行许可按有效租约计数：running 与 cancel_requested 中
            # 租约未过期者都占用许可；租约已过期（Worker 失联）或等待
            # 外部结果等不持租约的状态不占用，不能只统计 state=running
            effective_leases = conn.execute(
                "SELECT COUNT(*) FROM tasks"
                " WHERE state IN (?, ?)"
                " AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (TaskStatus.RUNNING.value, TaskStatus.CANCEL_REQUESTED.value, now),
            ).fetchone()[0]
            if effective_leases >= task_state.MAX_RUNNING:
                return None

            if task_type is None:
                row = conn.execute(
                    f"SELECT {_TASK_COLUMNS} FROM tasks WHERE state = ?"
                    " ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1",
                    (TaskStatus.QUEUED.value,),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT {_TASK_COLUMNS} FROM tasks"
                    " WHERE state = ? AND task_type = ?"
                    " ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1",
                    (TaskStatus.QUEUED.value, task_type),
                ).fetchone()
            if row is None:
                return None

            task = self._to_entity(row)
            conn.execute(
                "UPDATE tasks SET state = ?, lease_owner = ?, lease_expires_at = ?,"
                " heartbeat_at = ?, attempt_count = attempt_count + 1,"
                " started_at = COALESCE(started_at, ?)"
                " WHERE id = ?",
                (
                    TaskStatus.RUNNING.value, worker_id,
                    _lease_expiry_iso(task_state.LEASE_DURATION_SECONDS),
                    now, now, task.id,
                ),
            )
            self._insert_event(
                conn,
                task_id=task.id,
                event_type=_EVENT_STATE_CHANGED,
                state=TaskStatus.RUNNING,
                stage=task.stage,
                attempt_count=task.attempt_count + 1,
                worker=worker_id,
                created_at=now,
            )
            return self._to_entity(self._get_row(conn, task.id))

        return run_in_transaction(self._conn, _claim, f"领取任务 {worker_id}")

    def heartbeat(self, task_id: str, worker_id: str) -> Task:
        def _heartbeat(conn) -> Task:
            self._require_leased_task(conn, task_id, worker_id)
            conn.execute(
                "UPDATE tasks SET lease_expires_at = ?, heartbeat_at = ?"
                " WHERE id = ?",
                (_lease_expiry_iso(task_state.LEASE_DURATION_SECONDS),
                 utc_now_iso(), task_id),
            )
            return self._to_entity(self._get_row(conn, task_id))

        return run_in_transaction(self._conn, _heartbeat, f"任务心跳 {task_id}")

    def update_stage(
        self,
        task_id: str,
        worker_id: str,
        *,
        stage: TaskStage,
        progress: float | None = None,
        checkpoint_json: str | None = None,
    ) -> Task:
        def _update(conn) -> Task:
            task = self._require_leased_task(conn, task_id, worker_id)
            # 进入与当前不同的阶段视为新阶段开始执行：执行序号置 1，
            # 当前阶段的自动重试预算重置；同阶段重复上报（如批次间
            # 进度刷新）不改变任何计数
            if task.stage != stage:
                stage_attempt = 1
                retry_count = 0
            else:
                stage_attempt = task.stage_attempt
                retry_count = task.retry_count
            conn.execute(
                "UPDATE tasks SET stage = ?, stage_attempt = ?, retry_count = ?,"
                " progress = COALESCE(?, progress),"
                " checkpoint_json = COALESCE(?, checkpoint_json)"
                " WHERE id = ?",
                (stage.value, stage_attempt, retry_count, progress,
                 checkpoint_json, task_id),
            )
            return self._to_entity(self._get_row(conn, task_id))

        return run_in_transaction(
            self._conn, _update, f"更新任务阶段 {task_id}"
        )

    def complete_stage(
        self, task_id: str, worker_id: str, *, stage: TaskStage
    ) -> Task:
        def _complete(conn) -> Task:
            task = self._require_leased_task(conn, task_id, worker_id)
            if task.stage != stage:
                raise TaskStateConflictError(
                    f"任务 {task_id} 当前阶段为"
                    f" {task.stage.value if task.stage else '无'}，"
                    f"与完成目标 {stage.value} 不一致"
                )
            # 阶段成功后执行序号与该阶段的重试预算一并归零；
            # 下一阶段的开始由后续阶段写入表达
            conn.execute(
                "UPDATE tasks SET stage_attempt = 0, retry_count = 0 WHERE id = ?",
                (task_id,),
            )
            return self._to_entity(self._get_row(conn, task_id))

        return run_in_transaction(
            self._conn, _complete, f"完成任务阶段 {task_id}"
        )

    def schedule_retry(
        self,
        task_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
    ) -> Task:
        def _schedule(conn) -> Task:
            task = self._require_leased_task(conn, task_id, worker_id)
            if task.state not in (TaskStatus.RUNNING, TaskStatus.WAITING_EXTERNAL):
                raise TaskStateConflictError(
                    f"任务 {task_id} 状态为 {task.state.value}，不能安排自动重试"
                )
            now = utc_now_iso()
            # 当前阶段重试预算耗尽，或累计重试执行达到硬上限：
            # 不再安排下一次自动重试，转入失败终态
            if (
                task.retry_count >= task.max_retries
                or task.total_attempt_count >= task_state.MAX_TOTAL_ATTEMPTS
            ):
                return self._fail_in_transaction(
                    conn, task, error_code=error_code,
                    error_message=error_message, now=now,
                )

            retry_count = task.retry_count + 1
            backoff = task_state.retry_backoff_seconds(retry_count)
            next_retry_at = (
                datetime.now(timezone.utc) + timedelta(seconds=backoff)
            ).isoformat(timespec="seconds")
            # 执行序号与累计重试执行在安排时预增（该次再执行已确定）；
            # 释放租约让出执行许可，重试到期后经排队重新领取
            conn.execute(
                "UPDATE tasks SET state = ?, retry_count = ?,"
                " stage_attempt = stage_attempt + 1,"
                " total_attempt_count = total_attempt_count + 1,"
                " next_retry_at = ?, lease_owner = NULL, lease_expires_at = NULL,"
                " heartbeat_at = NULL, error_code = ?, error_message = ?"
                " WHERE id = ?",
                (
                    TaskStatus.RETRY_WAITING.value, retry_count, next_retry_at,
                    error_code, error_message, task_id,
                ),
            )
            self._insert_event(
                conn,
                task_id=task_id,
                event_type=_EVENT_STATE_CHANGED,
                state=TaskStatus.RETRY_WAITING,
                stage=task.stage,
                attempt_count=task.attempt_count,
                worker=task.lease_owner,
                created_at=now,
                checkpoint_json=task.checkpoint_json,
                error_code=error_code,
                detail_json=json.dumps(
                    {"retry_count": retry_count, "backoff_seconds": backoff},
                    ensure_ascii=False,
                ),
            )
            return self._to_entity(self._get_row(conn, task_id))

        return run_in_transaction(
            self._conn, _schedule, f"安排任务重试 {task_id}"
        )

    def fail_task(
        self,
        task_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
    ) -> Task:
        def _fail(conn) -> Task:
            task = self._require_leased_task(conn, task_id, worker_id)
            return self._fail_in_transaction(
                conn, task, error_code=error_code, error_message=error_message,
                now=utc_now_iso(),
            )

        return run_in_transaction(self._conn, _fail, f"任务置失败 {task_id}")

    def request_cancel(self, task_id: str) -> Task:
        def _request(conn) -> Task:
            row = self._get_row(conn, task_id)
            if row is None:
                raise EntityNotFoundError(f"任务不存在: {task_id}")
            task = self._to_entity(row)
            if task.state in task_state.TERMINAL_STATES:
                raise TaskStateConflictError(
                    f"任务 {task_id} 已进入终态 {task.state.value}，不可取消"
                )
            # 幂等：已在等待取消时直接返回当前状态
            if task.state is TaskStatus.CANCEL_REQUESTED:
                return task

            now = utc_now_iso()
            if task.state in (TaskStatus.QUEUED, TaskStatus.WAITING_USER):
                # 尚未进入执行：立即取消并完成收尾
                task_state.require_valid_transition(task.state, TaskStatus.CANCELLED)
                conn.execute(
                    "UPDATE tasks SET state = ?, finished_at = ?,"
                    " lease_owner = NULL, lease_expires_at = NULL,"
                    " heartbeat_at = NULL WHERE id = ?",
                    (TaskStatus.CANCELLED.value, now, task_id),
                )
                self._insert_event(
                    conn, task_id=task_id, event_type=_EVENT_STATE_CHANGED,
                    state=TaskStatus.CANCELLED, stage=task.stage,
                    attempt_count=task.attempt_count, worker=task.lease_owner,
                    created_at=now,
                )
            else:
                # 执行中/等待中/等待重试：先写取消请求，Worker 到安全
                # 检查点后完成取消；租约保留以维持执行许可口径
                task_state.require_valid_transition(
                    task.state, TaskStatus.CANCEL_REQUESTED
                )
                conn.execute(
                    "UPDATE tasks SET state = ?, cancel_requested_at = ?"
                    " WHERE id = ?",
                    (TaskStatus.CANCEL_REQUESTED.value, now, task_id),
                )
                self._insert_event(
                    conn, task_id=task_id, event_type=_EVENT_STATE_CHANGED,
                    state=TaskStatus.CANCEL_REQUESTED, stage=task.stage,
                    attempt_count=task.attempt_count, worker=task.lease_owner,
                    created_at=now, checkpoint_json=task.checkpoint_json,
                )
            return self._to_entity(self._get_row(conn, task_id))

        return run_in_transaction(self._conn, _request, f"请求取消任务 {task_id}")

    def cancel_at_checkpoint(self, task_id: str, worker_id: str) -> Task:
        def _cancel(conn) -> Task:
            task = self._require_leased_task(conn, task_id, worker_id)
            if task.state is not TaskStatus.CANCEL_REQUESTED:
                raise TaskStateConflictError(
                    f"任务 {task_id} 状态为 {task.state.value}，不在等待取消"
                )
            now = utc_now_iso()
            conn.execute(
                "UPDATE tasks SET state = ?, finished_at = ?,"
                " lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL"
                " WHERE id = ?",
                (TaskStatus.CANCELLED.value, now, task_id),
            )
            self._insert_event(
                conn, task_id=task_id, event_type=_EVENT_STATE_CHANGED,
                state=TaskStatus.CANCELLED, stage=task.stage,
                attempt_count=task.attempt_count, worker=worker_id,
                created_at=now, checkpoint_json=task.checkpoint_json,
            )
            return self._to_entity(self._get_row(conn, task_id))

        return run_in_transaction(
            self._conn, _cancel, f"完成任务取消 {task_id}"
        )

    def promote_due_retries(self) -> int:
        def _promote(conn) -> int:
            now = utc_now_iso()
            rows = conn.execute(
                "SELECT id, stage, attempt_count, checkpoint_json FROM tasks"
                " WHERE state = ? AND next_retry_at IS NOT NULL AND next_retry_at <= ?",
                (TaskStatus.RETRY_WAITING.value, now),
            ).fetchall()
            promoted = 0
            for row in rows:
                # 退避到期：回到排队等待重新领取。当前阶段的重试预算
                # 保留（重试次数按阶段累计），到期时间完成使命后清空
                conn.execute(
                    "UPDATE tasks SET state = ?, next_retry_at = NULL WHERE id = ?",
                    (TaskStatus.QUEUED.value, row[0]),
                )
                self._insert_event(
                    conn, task_id=row[0], event_type=_EVENT_STATE_CHANGED,
                    state=TaskStatus.QUEUED,
                    stage=TaskStage(row[1]) if row[1] is not None else None,
                    attempt_count=row[2], worker=None, created_at=now,
                    checkpoint_json=row[3],
                )
                promoted += 1
            return promoted

        return run_in_transaction(self._conn, _promote, "提升到期重试任务")

    def retry_failed(
        self, task_id: str, *, idempotency_key: str | None = None
    ) -> Task:
        old = self.get(task_id)
        if old is None:
            raise EntityNotFoundError(f"任务不存在: {task_id}")
        if old.state is not TaskStatus.FAILED:
            raise TaskStateConflictError(
                f"任务 {task_id} 状态为 {old.state.value}，仅失败任务可手动重试"
            )
        # 原任务保持终态不复活；新任务携带派生来源，经 create 在单事务
        # 内完成容量检查与插入。错误码可重试性由调用方（接口层）把关
        return self.create(
            old.task_type,
            knowledge_base_id=old.knowledge_base_id,
            document_id=old.document_id,
            document_version_id=old.document_version_id,
            index_version_id=old.index_version_id,
            priority=old.priority,
            idempotency_key=idempotency_key,
            input_json=old.input_json,
            max_retries=old.max_retries,
            parent_task_id=old.id,
            retry_origin="manual",
        )

    def requeue_stale_running(self) -> int:
        def _requeue(conn) -> int:
            now = utc_now_iso()
            # 执行中与等待外部结果的任务同等待遇：租约失效（Worker 失联）
            # 即重排回排队。等待外部结果的任务恢复后按既有批次续跑轮询，
            # 不重复提交批次（批次事实持久化在外部任务表中）
            rows = conn.execute(
                "SELECT id, stage, attempt_count, checkpoint_json, lease_owner"
                " FROM tasks WHERE state IN (?, ?)"
                " AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
                (
                    TaskStatus.RUNNING.value,
                    TaskStatus.WAITING_EXTERNAL.value,
                    _stale_lease_cutoff_iso(),
                ),
            ).fetchall()
            for row in rows:
                # 仅清理租约并回到排队：处理阶段、进度、checkpoint 与
                # 执行计数全部保留，重新领取后从最近断点继续
                conn.execute(
                    "UPDATE tasks SET state = ?, lease_owner = NULL,"
                    " lease_expires_at = NULL, heartbeat_at = NULL WHERE id = ?",
                    (TaskStatus.QUEUED.value, row[0]),
                )
                self._insert_event(
                    conn,
                    task_id=row[0],
                    event_type=_EVENT_STATE_CHANGED,
                    state=TaskStatus.QUEUED,
                    stage=TaskStage(row[1]) if row[1] is not None else None,
                    attempt_count=row[2],
                    worker=None,
                    created_at=now,
                    checkpoint_json=row[3],
                    detail_json=json.dumps(
                        {"interrupted": True, "lease_owner": row[4]},
                        ensure_ascii=False,
                    ),
                )
            return len(rows)

        return run_in_transaction(self._conn, _requeue, "重排中断执行任务")

    def finish_stale_cancel_requests(self) -> int:
        def _finish(conn) -> int:
            now = utc_now_iso()
            rows = conn.execute(
                "SELECT id, stage, attempt_count, checkpoint_json,"
                " cancel_requested_at FROM tasks WHERE state = ?"
                " AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
                (TaskStatus.CANCEL_REQUESTED.value, _stale_lease_cutoff_iso()),
            ).fetchall()
            for row in rows:
                # 原 Worker 已失联，无法到达检查点收尾：由恢复流程
                # 直接完成取消（staging 物理清理由后续清理任务承担）
                conn.execute(
                    "UPDATE tasks SET state = ?, finished_at = ?,"
                    " lease_owner = NULL, lease_expires_at = NULL,"
                    " heartbeat_at = NULL WHERE id = ?",
                    (TaskStatus.CANCELLED.value, now, row[0]),
                )
                self._insert_event(
                    conn,
                    task_id=row[0],
                    event_type=_EVENT_STATE_CHANGED,
                    state=TaskStatus.CANCELLED,
                    stage=TaskStage(row[1]) if row[1] is not None else None,
                    attempt_count=row[2],
                    worker=None,
                    created_at=now,
                    checkpoint_json=row[3],
                    detail_json=json.dumps(
                        {
                            "finished_by_recovery": True,
                            "cancel_requested_at": row[4],
                        },
                        ensure_ascii=False,
                    ),
                )
            return len(rows)

        return run_in_transaction(self._conn, _finish, "收尾失效取消请求")

    def recover_interrupted_tasks(self) -> dict[str, int]:
        """恢复被中断的任务，返回各类处理数量。

        供进程启动时与周期巡检调用；三个动作各自单事务且均可重复
        执行（幂等），重复调用时未变化的部分返回 0
        """
        promoted = self.promote_due_retries()
        requeued = self.requeue_stale_running()
        finished = self.finish_stale_cancel_requests()
        return {
            "retries_promoted": promoted,
            "stale_running_requeued": requeued,
            "stale_cancels_finished": finished,
        }

    @staticmethod
    def _require_leased_task(conn, task_id: str, worker_id: str) -> Task:
        """读取任务并校验租约：任务存在、持有者匹配且租约未过期。

        过期判断基于 UTC 文本比较（时间字段统一为同时刻区格式的
        ISO-8601，字典序即时间序）
        """
        row = SQLiteTaskRepository._get_row(conn, task_id)
        if row is None:
            raise EntityNotFoundError(f"任务不存在: {task_id}")
        task = SQLiteTaskRepository._to_entity(row)
        if (
            task.lease_owner != worker_id
            or task.lease_expires_at is None
            or task.lease_expires_at <= utc_now_iso()
        ):
            raise TaskLeaseLostError(f"任务 {task_id} 租约无效或已丢失")
        return task

    @staticmethod
    def _fail_in_transaction(
        conn,
        task: Task,
        *,
        error_code: str,
        error_message: str | None,
        now: str,
    ) -> Task:
        """在当前事务内把任务置为失败终态并释放租约。

        调用方负责前置校验（租约有效、状态允许失败）；失败是终态，
        原任务不复活，恢复手段为手动重试创建新任务
        """
        task_state.require_valid_transition(task.state, TaskStatus.FAILED)
        conn.execute(
            "UPDATE tasks SET state = ?, finished_at = ?, error_code = ?,"
            " error_message = ?, lease_owner = NULL, lease_expires_at = NULL,"
            " heartbeat_at = NULL WHERE id = ?",
            (TaskStatus.FAILED.value, now, error_code, error_message, task.id),
        )
        SQLiteTaskRepository._insert_event(
            conn,
            task_id=task.id,
            event_type=_EVENT_STATE_CHANGED,
            state=TaskStatus.FAILED,
            stage=task.stage,
            attempt_count=task.attempt_count,
            worker=task.lease_owner,
            created_at=now,
            checkpoint_json=task.checkpoint_json,
            error_code=error_code,
        )
        return SQLiteTaskRepository._to_entity(
            SQLiteTaskRepository._get_row(conn, task.id)
        )

    def _record_rejected_transition(self, task: Task, target_state: TaskStatus) -> None:
        """把被拒绝的迁移作为审计事件落库（独立事务，不随异常回滚）"""
        detail = json.dumps(
            {
                "from_state": task.state.value,
                "to_state": target_state.value,
            },
            ensure_ascii=False,
        )

        def _record(conn) -> None:
            self._insert_event(
                conn,
                task_id=task.id,
                event_type=_EVENT_TRANSITION_REJECTED,
                state=task.state,
                stage=task.stage,
                attempt_count=task.attempt_count,
                worker=task.lease_owner,
                created_at=utc_now_iso(),
                checkpoint_json=task.checkpoint_json,
                error_code=_STATE_CONFLICT_CODE,
                detail_json=detail,
            )

        run_in_transaction(
            self._conn, _record, f"记录任务 {task.id} 的非法迁移事件"
        )

    @staticmethod
    def _insert_event(
        conn,
        *,
        task_id: str,
        event_type: str,
        state: TaskStatus,
        stage: TaskStage | None,
        attempt_count: int,
        worker: str | None,
        created_at: str,
        duration_ms: int | None = None,
        checkpoint_json: str | None = None,
        error_code: str | None = None,
        detail_json: str | None = None,
    ) -> None:
        """追加一条审计事件（委托模块级共享实现，导入仓储复用同一写入）"""
        insert_task_event(
            conn,
            task_id=task_id,
            event_type=event_type,
            state=state,
            stage=stage,
            attempt_count=attempt_count,
            worker=worker,
            created_at=created_at,
            duration_ms=duration_ms,
            checkpoint_json=checkpoint_json,
            error_code=error_code,
            detail_json=detail_json,
        )

    @staticmethod
    def _get_row(conn, task_id: str):
        """按 ID 读取任务原始行"""
        return conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    @staticmethod
    def _to_entity(row) -> Task:
        """把查询行转换为领域实体"""
        return Task(
            id=row[0],
            task_type=row[1],
            knowledge_base_id=row[2],
            document_id=row[3],
            document_version_id=row[4],
            index_version_id=row[5],
            state=TaskStatus(row[6]),
            stage=TaskStage(row[7]) if row[7] is not None else None,
            progress=row[8],
            priority=row[9],
            idempotency_key=row[10],
            retry_count=row[11],
            max_retries=row[12],
            attempt_count=row[13],
            stage_attempt=row[14],
            total_attempt_count=row[15],
            next_retry_at=row[16],
            lease_owner=row[17],
            lease_expires_at=row[18],
            heartbeat_at=row[19],
            cancel_requested_at=row[20],
            checkpoint_json=row[21],
            parent_task_id=row[22],
            retry_origin=row[23],
            error_code=row[24],
            error_message=row[25],
            input_json=row[26],
            created_at=row[27],
            started_at=row[28],
            finished_at=row[29],
        )

    @staticmethod
    def _event_to_entity(row) -> TaskEvent:
        """把事件查询行转换为领域实体"""
        return TaskEvent(
            id=row[0],
            task_id=row[1],
            event_type=row[2],
            state=TaskStatus(row[3]),
            stage=TaskStage(row[4]) if row[4] is not None else None,
            attempt_count=row[5],
            worker=row[6],
            created_at=row[7],
            duration_ms=row[8],
            checkpoint_json=row[9],
            error_code=row[10],
            detail_json=row[11],
        )
