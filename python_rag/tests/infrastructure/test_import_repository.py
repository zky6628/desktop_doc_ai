# -*- coding: utf-8 -*-
"""导入仓储测试：单事务建立文档/版本/任务的约束与回滚"""
import pytest

from app.domain.errors import (
    DuplicateActiveContentError,
    EntityNotFoundError,
    TaskQueueFullError,
)
from app.domain.ids import uuid7
from app.domain.task_state import MAX_PENDING
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
    """临时库 + 导入/任务仓储 + 预置知识库"""
    db_path, _ = fresh_db(tmp_path, "import.db")
    conn = connect(db_path)
    kb_id = insert_kb(conn, "导入测试库")
    yield conn, kb_id
    conn.close()


def _import_kwargs(sha: str) -> dict:
    """构造一份固定的导入参数（哈希按用例区分）"""
    return {
        "display_name": "手册.txt",
        "source_path": "staging-path",
        "source_sha256": sha,
        "mime_type": "text/plain",
        "size_bytes": 12,
        "parser_mode": "local",
        "parser_route_json": '{"mode":"local"}',
    }


class TestSingleTransactionImport:
    """单事务导入的基本行为"""

    def test_creates_document_version_and_task(self, runtime):
        conn, kb_id = runtime
        repo = SQLiteImportRepository(conn)
        outcome = repo.create_import(kb_id, **_import_kwargs("a" * 64))
        assert outcome.reused_existing_document is False
        assert outcome.task.task_type == "import"
        assert outcome.task.state.value == "queued"
        assert outcome.task.document_id == outcome.document_id
        assert outcome.task.document_version_id == outcome.document_version_id

        row = conn.execute(
            "SELECT version_no, status, source_sha256 FROM document_versions"
            " WHERE id = ?",
            (outcome.document_version_id,),
        ).fetchone()
        assert row[0] == 1
        assert row[1] == "pending"
        assert row[2] == "a" * 64

        events = conn.execute(
            "SELECT event_type FROM task_events WHERE task_id = ?",
            (outcome.task.id,),
        ).fetchall()
        assert [event[0] for event in events] == ["created"]

    def test_new_version_reuses_document_and_increments_no(self, runtime):
        conn, kb_id = runtime
        repo = SQLiteImportRepository(conn)
        first = repo.create_import(
            kb_id, **_import_kwargs("b" * 64), duplicate_policy="new_version"
        )
        second = repo.create_import(
            kb_id, **_import_kwargs("b" * 64), duplicate_policy="new_version"
        )
        assert second.reused_existing_document is True
        assert second.document_id == first.document_id
        rows = conn.execute(
            "SELECT version_no FROM document_versions WHERE document_id = ?"
            " ORDER BY version_no",
            (first.document_id,),
        ).fetchall()
        assert [row[0] for row in rows] == [1, 2]

    def test_idempotency_replay_returns_existing_task(self, runtime):
        conn, kb_id = runtime
        repo = SQLiteImportRepository(conn)
        first = repo.create_import(
            kb_id, **_import_kwargs("c" * 64), idempotency_key="key-1"
        )
        replay = repo.create_import(
            kb_id, **_import_kwargs("c" * 64), idempotency_key="key-1"
        )
        assert replay.task.id == first.task.id
        versions = conn.execute(
            "SELECT COUNT(*) FROM document_versions WHERE document_id = ?",
            (first.document_id,),
        ).fetchone()[0]
        assert versions == 1


class TestImportRejection:
    """导入的拒绝路径与零残留"""

    def test_skip_policy_rejects_duplicate(self, runtime):
        conn, kb_id = runtime
        repo = SQLiteImportRepository(conn)
        repo.create_import(kb_id, **_import_kwargs("d" * 64))
        with pytest.raises(DuplicateActiveContentError):
            repo.create_import(kb_id, **_import_kwargs("d" * 64))
        # 拒绝不产生重复文档/版本/任务
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    def test_missing_kb_rejected(self, tmp_path):
        db_path, _ = fresh_db(tmp_path, "nokb.db")
        conn = connect(db_path)
        try:
            repo = SQLiteImportRepository(conn)
            with pytest.raises(EntityNotFoundError):
                repo.create_import(kb_id=uuid7(), **_import_kwargs("e" * 64))
        finally:
            conn.close()

    def test_queue_full_rolls_back_document_and_version(self, runtime):
        conn, kb_id = runtime
        # 预填队列到 pending 上限：导入事务在任务创建前被容量检查拒绝
        tasks = SQLiteTaskRepository(conn)
        for index in range(MAX_PENDING):
            tasks.create("import", idempotency_key=f"fill-{index}")
        documents_before = conn.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]
        with pytest.raises(TaskQueueFullError):
            SQLiteImportRepository(conn).create_import(
                kb_id, **_import_kwargs("f" * 64)
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            == documents_before
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM document_versions"
        ).fetchone()[0] == 0
