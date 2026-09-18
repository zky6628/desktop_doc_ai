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
            # 串行化后读到的计数即为提交时刻事实，杜绝并发超限。
            # running 的物理执行许可由领取逻辑做租约感知计数，
            # 创建阶段不拦截，因此已有 3 个 running 时新任务仍可
            # 创建并保持 queued 排队
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
                " total_attempt_count = total_attempt_count + 1,"
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
            # 进入与当前不同的阶段视为新阶段开始执行；同阶段重复上报
            # （如批次间进度刷新）不改变执行计数
            stage_attempt = 1 if task.stage != stage else task.stage_attempt
            conn.execute(
                "UPDATE tasks SET stage = ?, stage_attempt = ?,"
                " progress = COALESCE(?, progress),"
                " checkpoint_json = COALESCE(?, checkpoint_json)"
                " WHERE id = ?",
                (stage.value, stage_attempt, progress, checkpoint_json, task_id),
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
            # 阶段成功后执行计数归零；下一阶段的开始由后续阶段写入表达
            conn.execute(
                "UPDATE tasks SET stage_attempt = 0 WHERE id = ?", (task_id,)
            )
            return self._to_entity(self._get_row(conn, task_id))

        return run_in_transaction(
            self._conn, _complete, f"完成任务阶段 {task_id}"
        )

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
        """追加一条审计事件"""
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
