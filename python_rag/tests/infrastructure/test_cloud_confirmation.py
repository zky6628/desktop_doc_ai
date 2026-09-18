# -*- coding: utf-8 -*-
"""云端确认服务测试：批准/拒绝路径与冲突拒绝"""
import pytest

from app.domain.entities import TaskStage, TaskStatus
from app.domain.errors import ConfirmationConflictError, EntityNotFoundError
from app.infrastructure.ingest import confirm_cloud_parsing
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories.import_repository import (
    SQLiteImportRepository,
)
from app.infrastructure.sqlite.repositories.task_repository import (
    SQLiteTaskRepository,
)
from tests.infrastructure.schema_helpers import fresh_db, insert_kb


@pytest.fixture()
def runtime(tmp_path):
    """临时库与仓储，返回 (连接, 导入仓储, 任务仓储, kb_id)"""
    db_path, _ = fresh_db(tmp_path, "confirm.db")
    conn = connect(db_path)
    kb_id = insert_kb(conn, "确认测试库")
    imports = SQLiteImportRepository(conn)
    tasks = SQLiteTaskRepository(conn)
    yield conn, imports, tasks, kb_id
    conn.close()


def _make_waiting_task(imports, tasks, kb_id, sha: str):
    """构造一个处于等待确认状态（停在路由阶段）的任务

    导入任务从 queued 出发：领取后进入 running，再转入
    waiting_user 并停留在路由阶段，即用户确认界面对应的状态
    """
    outcome = imports.create_import(
        kb_id,
        display_name=f"{sha[:6]}.pdf",
        source_path="staging-path",
        source_sha256=sha,
        mime_type="application/pdf",
        size_bytes=10,
        parser_mode="local",
    )
    claimed = tasks.claim_next("worker-confirm")
    assert claimed is not None and claimed.id == outcome.task.id
    return tasks.transition(
        outcome.task.id, TaskStatus.WAITING_USER, stage=TaskStage.ROUTING_PARSER
    )


class TestCloudConfirmation:
    """确认答复的业务路径"""

    def test_approve_returns_to_queued_for_cloud_submission(self, runtime):
        _, imports, tasks, kb_id = runtime
        waiting = _make_waiting_task(imports, tasks, kb_id, "a" * 64)
        confirmed = confirm_cloud_parsing(tasks, waiting.id, "approve")
        assert confirmed.state is TaskStatus.QUEUED
        assert confirmed.stage is TaskStage.SUBMITTING_CLOUD

    def test_reject_cancels_immediately(self, runtime):
        _, imports, tasks, kb_id = runtime
        waiting = _make_waiting_task(imports, tasks, kb_id, "b" * 64)
        confirmed = confirm_cloud_parsing(tasks, waiting.id, "reject")
        assert confirmed.state is TaskStatus.CANCELLED
        assert confirmed.finished_at is not None

    def test_unknown_decision_rejected(self, runtime):
        _, imports, tasks, kb_id = runtime
        waiting = _make_waiting_task(imports, tasks, kb_id, "c" * 64)
        with pytest.raises(ValueError):
            confirm_cloud_parsing(tasks, waiting.id, "maybe")

    def test_missing_task_rejected(self, runtime):
        _, _, tasks, _ = runtime
        with pytest.raises(EntityNotFoundError):
            confirm_cloud_parsing(tasks, "nonexistent", "approve")

    def test_non_waiting_task_conflict(self, runtime):
        _, imports, tasks, kb_id = runtime
        outcome = imports.create_import(
            kb_id,
            display_name="排队中.pdf",
            source_path="staging-path",
            source_sha256="d" * 64,
            parser_mode="local",
        )
        # queued 状态未经 Worker 领取与解析，不属于可确认状态
        with pytest.raises(ConfirmationConflictError):
            confirm_cloud_parsing(tasks, outcome.task.id, "approve")

    def test_repeated_confirmation_conflict(self, runtime):
        _, imports, tasks, kb_id = runtime
        waiting = _make_waiting_task(imports, tasks, kb_id, "e" * 64)
        confirm_cloud_parsing(tasks, waiting.id, "approve")
        with pytest.raises(ConfirmationConflictError):
            confirm_cloud_parsing(tasks, waiting.id, "approve")
