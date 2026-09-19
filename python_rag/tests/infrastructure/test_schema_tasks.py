# -*- coding: utf-8 -*-
"""任务引擎表 schema 测试：CHECK 约束、外键行为与唯一键"""
import sqlite3

import pytest

from app.infrastructure.sqlite.connection import connect

from .schema_helpers import (
    fresh_db,
    insert_document,
    insert_external_task,
    insert_index_version,
    insert_kb,
    insert_task,
    insert_task_event,
    insert_version,
)


@pytest.fixture()
def db(tmp_path):
    """应用全部迁移并开启外键的临时库连接"""
    db_path, applied = fresh_db(tmp_path, name="schema_tasks.db")
    assert applied == 6
    conn = connect(db_path)
    yield conn
    conn.close()


def test_state_check_constraint(db):
    """state 仅接受九个生命周期取值，非法值被拒绝"""
    with pytest.raises(sqlite3.IntegrityError):
        insert_task(db, state="pending")


def test_stage_check_constraint(db):
    """stage 仅接受十六个处理阶段取值，非法值被拒绝"""
    with pytest.raises(sqlite3.IntegrityError):
        insert_task(db, stage="parsing")


def test_progress_check_constraint(db):
    """progress 限定在 0..1，越界被拒绝"""
    with pytest.raises(sqlite3.IntegrityError):
        insert_task(db, progress=1.5)
    with pytest.raises(sqlite3.IntegrityError):
        insert_task(db, progress=-0.1)


def test_retry_origin_check_constraint(db):
    """retry_origin 仅接受 manual（或空），其他派生来源被拒绝"""
    with pytest.raises(sqlite3.IntegrityError):
        insert_task(db, retry_origin="auto")


def test_task_fk_set_null_on_referenced_delete(tmp_path):
    """删除知识库/文档/版本/索引/父任务后任务存活且引用置空（审计保留）"""
    db_path, _ = fresh_db(tmp_path, name="schema_tasks_fk.db")
    conn = connect(db_path)
    try:
        kb_id = insert_kb(conn, "kb")
        doc_id = insert_document(conn, kb_id, "a" * 64)
        version_id = insert_version(conn, doc_id)
        index_id = insert_index_version(conn, version_id)
        parent_id = insert_task(conn, task_type="import")
        child_id = insert_task(
            conn,
            task_type="import",
            kb_id=kb_id,
            document_id=doc_id,
            document_version_id=version_id,
            index_version_id=index_id,
            parent_task_id=parent_id,
            retry_origin="manual",
        )

        # 按依赖顺序删除：索引 -> 版本 -> 文档 -> 知识库 -> 父任务
        conn.execute("DELETE FROM index_versions WHERE id = ?", (index_id,))
        conn.execute("DELETE FROM document_versions WHERE id = ?", (version_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.execute("DELETE FROM knowledge_bases WHERE id = ?", (kb_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (parent_id,))

        row = conn.execute(
            "SELECT knowledge_base_id, document_id, document_version_id,"
            " index_version_id, parent_task_id, retry_origin"
            " FROM tasks WHERE id = ?",
            (child_id,),
        ).fetchone()
        assert row == (None, None, None, None, None, "manual")
    finally:
        conn.close()


def test_task_event_restricts_task_deletion(db):
    """存在审计事件的任务不可删除（任务与事件同为审计事实）"""
    task_id = insert_task(db)
    insert_task_event(db, task_id)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def test_idempotency_key_unique_among_non_null(db):
    """幂等键唯一：同键拒绝二次插入；多个无键任务允许并存"""
    insert_task(db, idempotency_key="idem-1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_task(db, idempotency_key="idem-1")
    insert_task(db)
    insert_task(db)


def test_external_task_unique_key(db):
    """外部任务稳定唯一键为 (provider, provider_batch_ref, source_ref)；
    provider_task_id 可空且不参与唯一约束"""
    task_id = insert_task(db)
    insert_external_task(db, task_id, provider_task_id=None)
    with pytest.raises(sqlite3.IntegrityError):
        insert_external_task(db, task_id, provider_task_id=None)

    # 批次或源引用不同即可并存，provider_task_id 有无均不影响
    insert_external_task(db, task_id, provider_batch_ref="batch-2")
    insert_external_task(db, task_id, source_ref="source-2", provider_task_id="remote-1")

    # 同一稳定键即使携带不同 provider_task_id 仍然冲突
    with pytest.raises(sqlite3.IntegrityError):
        insert_external_task(db, task_id, provider_task_id="remote-again")


def test_external_task_requires_existing_task(db):
    """外部任务必须挂接在已存在的任务上"""
    with pytest.raises(sqlite3.IntegrityError):
        insert_external_task(db, "01900000-0000-7000-8000-000000000000")
