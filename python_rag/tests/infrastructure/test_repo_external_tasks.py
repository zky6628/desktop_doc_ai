# -*- coding: utf-8 -*-
"""外部任务仓储测试：幂等登记、轮询累加与结果记录"""
import pytest

from app.domain.errors import EntityNotFoundError
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories.external_task_repository import (
    SQLiteExternalTaskRepository,
)
from app.infrastructure.sqlite.repositories.task_repository import (
    SQLiteTaskRepository,
)
from tests.infrastructure.schema_helpers import fresh_db, insert_kb


@pytest.fixture()
def runtime(tmp_path):
    """临时库 + 任务/外部任务仓储 + 预置一个导入任务"""
    db_path, _ = fresh_db(tmp_path, "external.db")
    conn = connect(db_path)
    kb_id = insert_kb(conn, "外部任务测试库")
    tasks = SQLiteTaskRepository(conn)
    task = tasks.create("import", knowledge_base_id=kb_id)
    externals = SQLiteExternalTaskRepository(conn)
    yield conn, tasks, externals, task.id
    conn.close()


def _register(externals, task_id: str, source_ref: str = "version-1"):
    return externals.register(
        task_id=task_id,
        provider="mineru",
        provider_batch_ref="batch-1",
        source_ref=source_ref,
        upload_url_expires_at="2026-09-20T00:00:00+00:00",
        request_summary_json='{"files":1}',
    )


class TestRegistration:
    """幂等登记与查询"""

    def test_register_creates_record(self, runtime):
        _, _, externals, task_id = runtime
        external = _register(externals, task_id)
        assert external.task_id == task_id
        assert external.provider == "mineru"
        assert external.provider_batch_ref == "batch-1"
        assert external.source_ref == "version-1"
        assert external.upload_url_expires_at == "2026-09-20T00:00:00+00:00"
        assert external.poll_count == 0
        # 登记即表示批次已提交：初始供应方侧状态为 submitted
        assert external.state == "submitted"

    def test_duplicate_register_returns_same_record(self, runtime):
        _, _, externals, task_id = runtime
        first = _register(externals, task_id)
        second = _register(externals, task_id)
        assert second.id == first.id
        rows = externals.list_by_task(task_id)
        assert len(rows) == 1

    def test_get_by_refs(self, runtime):
        _, _, externals, task_id = runtime
        _register(externals, task_id)
        found = externals.get_by_refs("mineru", "batch-1", "version-1")
        assert found is not None and found.task_id == task_id
        assert externals.get_by_refs("mineru", "batch-1", "missing") is None

    def test_list_by_task_preserves_registration_order(self, runtime):
        _, _, externals, task_id = runtime
        _register(externals, task_id, source_ref="v-1")
        _register(externals, task_id, source_ref="v-2")
        rows = externals.list_by_task(task_id)
        assert [row.source_ref for row in rows] == ["v-1", "v-2"]


class TestPollAndResult:
    """轮询事实与结果记录"""

    def test_record_poll_increments_count(self, runtime):
        _, _, externals, task_id = runtime
        external = _register(externals, task_id)
        first = externals.record_poll(
            external.id, state="running", status_summary="running 1/2 pages"
        )
        second = externals.record_poll(
            external.id, state="running", status_summary="running 2/2 pages"
        )
        assert (first.poll_count, second.poll_count) == (1, 2)
        assert second.last_polled_at is not None
        assert second.provider_status_summary == "running 2/2 pages"

    def test_record_poll_keeps_first_provider_task_id(self, runtime):
        _, _, externals, task_id = runtime
        external = _register(externals, task_id)
        externals.record_poll(
            external.id, state="running", provider_task_id="pt-1"
        )
        again = externals.record_poll(external.id, state="pending")
        assert again.provider_task_id == "pt-1"

    def test_record_poll_missing_record_rejected(self, runtime):
        _, _, externals, _ = runtime
        with pytest.raises(EntityNotFoundError):
            externals.record_poll("nonexistent", state="done")

    def test_set_result_records_hash(self, runtime):
        _, _, externals, task_id = runtime
        external = _register(externals, task_id)
        externals.record_poll(external.id, state="done")
        updated = externals.set_result(
            external.id,
            result_sha256="a" * 64,
            expires_at="2026-09-21T00:00:00+00:00",
            state="done",
        )
        assert updated.result_sha256 == "a" * 64
        assert updated.expires_at == "2026-09-21T00:00:00+00:00"
        assert updated.state == "done"
