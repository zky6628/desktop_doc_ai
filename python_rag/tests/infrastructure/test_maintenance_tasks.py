# -*- coding: utf-8 -*-
"""维护任务 Worker 测试：清理/重建任务分派、失败补偿链与周期扫描

导入链路使用真实解析器与暂存文件，向量化使用确定性替身；清理失败
现场以包装适配器模拟文件占用，验证自动重试、有界补偿与旧索引零损伤。
"""
import json
import time
from datetime import datetime, timedelta, timezone

from app.domain.chunking import CHUNKING_OVERRIDE_SETTING_KEY
from app.domain.errors import EmbeddingTransientError
from app.domain.ids import uuid7

from .schema_helpers import BlockedDeleteVectorIndex
from .schema_helpers import import_file as _import_file
from .test_index_cleanup import _build_retired_index


class _FailingGateway:
    """首轮调用即抛瞬态错误的向量化替身（重建失败现场用）"""

    def embed_texts(self, texts):
        raise EmbeddingTransientError("供应商限流")


def _cleanup_tasks(env) -> list:
    """按创建顺序读取全部清理任务原始行"""
    return env.conn.execute(
        "SELECT id, state, parent_task_id, input_json FROM tasks"
        " WHERE task_type = 'cleanup' ORDER BY created_at, rowid"
    ).fetchall()


def _drive_to_terminal(env, worker, task_id: str):
    """驱动任务到达终态：重试等待则回写到期时间并提升，其余持续领取"""
    for _ in range(50):
        task = env.repos["tasks"].get(task_id)
        if task.state.value in ("succeeded", "failed", "cancelled"):
            return task
        if task.state.value == "retry_waiting":
            past = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            env.conn.execute(
                "UPDATE tasks SET next_retry_at = ? WHERE id = ?", (past, task_id)
            )
            assert env.repos["tasks"].promote_due_retries() == 1
        else:
            assert worker.process_next() is True
    raise AssertionError("任务未在预期轮次内到达终态")


def test_import_success_spawns_executable_cleanup_task(env):
    """导入成功后自动登记清理任务，领取执行即完成清扫并留审计摘要"""
    task_id = _import_file(env, "笔记.txt", "导入清理内容\n\n第二段".encode())
    worker = env.build()

    assert worker.process_next() is True
    assert env.repos["tasks"].get(task_id).state.value == "succeeded"
    rows = _cleanup_tasks(env)
    assert len(rows) == 1
    assert rows[0][1] == "queued"

    assert worker.process_next() is True

    rows = _cleanup_tasks(env)
    assert rows[0][1] == "succeeded"
    events = env.repos["tasks"].list_events(rows[0][0])
    summaries = [e for e in events if e.event_type == "cleanup_summary"]
    assert len(summaries) == 1
    payload = json.loads(summaries[0].detail_json)
    assert payload["failed"] == []


def test_cleanup_removes_retired_predecessor(env):
    """重建切换使旧索引退役：清理任务回收退役资产但保留索引行"""
    retired, version = _build_retired_index(env)
    worker = env.build()

    cleanup = env.repos["tasks"].create("cleanup")
    assert worker.process_next() is True

    finished = env.repos["tasks"].get(cleanup.id)
    assert finished.state.value == "succeeded"
    refetched = env.repos["indexes"].get(retired.id)
    assert refetched is not None
    assert refetched.status.value == "retired"
    assert env.vector_index.count_vectors(f"wb-idx-{retired.id}") == 0
    assert env.keyword_index.count_documents(f"fts-{retired.id}") == 0
    assert env.repos["chunks"].count_index_chunks(retired.id) == 0
    # 现役活动索引不受清理影响
    active_index = env.repos["indexes"].list_by_document_version(version.id)
    assert any(index.status.value == "active" for index in active_index)


def test_cleanup_transient_failure_retries_then_compensates(env):
    """清扫持续瞬态失败：自动重试预算耗尽转失败，补偿任务接续成功"""
    retired, _ = _build_retired_index(env)

    cleanup = env.repos["tasks"].create("cleanup")
    wrapped = BlockedDeleteVectorIndex(env.vector_index, {f"wb-idx-{retired.id}"})
    blocked_worker = env.build(vector_index=wrapped)

    finished = _drive_to_terminal(env, blocked_worker, cleanup.id)
    assert finished.state.value == "failed"
    assert finished.error_code == "CLEANUP_TRANSIENT"

    # 失败终态自动派生一层补偿任务
    rows = _cleanup_tasks(env)
    assert len(rows) == 2  # 失败的原始任务 + 补偿任务
    assert rows[-1][2] == cleanup.id
    assert json.loads(rows[-1][3])["compensation_depth"] == 1

    # 解除占用后补偿任务清扫成功
    wrapped.blocked.clear()
    finished = _drive_to_terminal(env, blocked_worker, rows[-1][0])
    assert finished.state.value == "succeeded"
    assert env.vector_index.count_vectors(f"wb-idx-{retired.id}") == 0


def test_cleanup_compensation_depth_capped(env):
    """永久不可清理：补偿任务按深度上限停止自动派生"""
    retired, _ = _build_retired_index(env)

    cleanup = env.repos["tasks"].create("cleanup")
    blocked_worker = env.build(
        vector_index=BlockedDeleteVectorIndex(
            env.vector_index, {f"wb-idx-{retired.id}"}
        )
    )

    rows = _cleanup_tasks(env)
    current_id = cleanup.id
    for depth in range(1, 4):
        finished = _drive_to_terminal(env, blocked_worker, current_id)
        assert finished.state.value == "failed"
        rows = _cleanup_tasks(env)
        assert len(rows) == 1 + depth  # 原始清理任务 + 各层补偿
        child = rows[-1]
        assert child[2] == current_id
        assert json.loads(child[3])["compensation_depth"] == depth
        current_id = child[0]

    # 深度达到上限：最后一次失败不再派生
    finished = _drive_to_terminal(env, blocked_worker, current_id)
    assert finished.state.value == "failed"
    assert len(_cleanup_tasks(env)) == 4  # 1 原始 + 3 层补偿，无第 4 层


def test_rebuild_task_rebuilds_and_switches(env):
    """重建任务：集合损坏后从切片事实重建双索引并原子切换"""
    task_id = _import_file(env, "笔记.txt", "重建内容\n\n第二段内容".encode())
    worker = env.build()
    assert worker.process_next() is True
    assert worker.process_next() is True  # 附带的清理任务

    version_id = env.repos["tasks"].get(task_id).document_version_id
    old_index = env.repos["indexes"].list_by_document_version(version_id)[0]
    env.vector_index.delete_collection(old_index.vector_collection)

    rebuild = env.repos["tasks"].create(
        "rebuild_index", document_version_id=version_id
    )
    assert worker.process_next() is True

    finished = env.repos["tasks"].get(rebuild.id)
    assert finished.state.value == "succeeded"
    indexes = env.repos["indexes"].list_by_document_version(version_id)
    assert len(indexes) == 2
    statuses = {index.id: index.status.value for index in indexes}
    assert statuses[old_index.id] == "retired"
    new_active = next(
        index for index in indexes if index.status.value == "active"
    )
    assert env.vector_index.count_vectors(new_active.vector_collection) == (
        new_active.chunk_count
    )
    assert env.keyword_index.count_documents(new_active.fts_namespace) == (
        new_active.chunk_count
    )
    version = env.repos["versions"].get(version_id)
    document = env.repos["documents"].get(version.document_id)
    assert document.active_document_version_id == version_id


def test_rebuild_failure_keeps_old_active_intact(env):
    """重建失败：任务转入重试等待，旧活动索引完好无损"""
    # 段落长度足以让参数变化改变切片聚合（缓存未命中是失败前提）
    long_text = "失败保护内容" * 60 + "\n\n" + "第二段内容" * 60
    task_id = _import_file(env, "笔记.txt", long_text.encode())
    worker = env.build()
    assert worker.process_next() is True
    assert worker.process_next() is True  # 附带的清理任务

    version_id = env.repos["tasks"].get(task_id).document_version_id
    old_index = env.repos["indexes"].list_by_document_version(version_id)[0]
    vectors_before = env.vector_index.count_vectors(old_index.vector_collection)

    # 改变切片参数使重建产生新切片（缓存未命中），嵌入失败才会发生
    env.repos["settings"].put(
        CHUNKING_OVERRIDE_SETTING_KEY,
        '{"child_chunk_chars":200,"parent_chunk_chars":500}',
    )
    rebuild = env.repos["tasks"].create(
        "rebuild_index", document_version_id=version_id
    )
    failing_worker = env.build(embedding_gateway=_FailingGateway())
    assert failing_worker.process_next() is True

    finished = env.repos["tasks"].get(rebuild.id)
    assert finished.state.value == "retry_waiting"
    assert finished.error_code == "EMBEDDING_TRANSIENT"
    indexes = {
        index.id: index
        for index in env.repos["indexes"].list_by_document_version(version_id)
    }
    assert indexes[old_index.id].status.value == "active"
    assert env.vector_index.count_vectors(old_index.vector_collection) == vectors_before
    # 失败重建的 staging 索引保留现场，等待补偿清理
    staging_ids = [i for i in indexes if i != old_index.id]
    assert all(indexes[i].status.value == "staging" for i in staging_ids)


def test_unknown_task_type_fails_internal(env):
    """未支持的任务类型：按内部错误转入失败终态"""
    task = env.repos["tasks"].create("frobnicate")
    worker = env.build()

    assert worker.process_next() is True

    failed = env.repos["tasks"].get(task.id)
    assert failed.state.value == "failed"
    assert failed.error_code == "INTERNAL_ERROR"


def test_periodic_scan_creates_cleanup_task_for_residue(env):
    """周期兜底扫描：存在孤儿残留时自动登记并执行清理任务"""
    env.vector_index.upsert_vectors(f"wb-idx-{uuid7()}", ["orphan"], [[1.0] * 4])
    worker = env.build()

    thread = worker.start_background()
    deadline = time.time() + 10
    states = []
    while time.time() < deadline:
        rows = _cleanup_tasks(env)
        states = [row[1] for row in rows]
        if states and all(state == "succeeded" for state in states):
            break
        time.sleep(0.05)
    worker.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert states and all(state == "succeeded" for state in states)
    assert env.vector_index.list_collections() == []
