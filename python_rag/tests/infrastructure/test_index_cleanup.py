# -*- coding: utf-8 -*-
"""派生索引补偿清理测试：退役/残留索引、孤儿资产、产物目录与清扫幂等

使用真实 SQLite 仓储与 Chroma 内存实例；保留期判定注入固定时间源，
在途任务守卫与失败记录逐项验证。
"""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import chromadb
import pytest

from app.domain.entities import TaskStatus
from app.domain.ids import uuid7
from app.domain.keyword import KeywordDocument
from app.infrastructure.keywordindex import JiebaTokenizer, SQLiteFtsKeywordIndex
from app.infrastructure.maintenance import IndexCleanupService
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteConfigRepository,
    SQLiteContentRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteIndexVersionRepository,
    SQLiteKnowledgeBaseRepository,
    SQLiteTaskRepository,
)
from app.infrastructure.vectorindex import ChromaVectorIndexAdapter

from .schema_helpers import BlockedDeleteVectorIndex, fresh_db
from .test_index_health import _LONG_PARAGRAPH, _build_index, _stage_version


class _FixedClock:
    """固定时间源：保留期判定不依赖真实时间（行侧用 SQL 回写对齐）"""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        return self.now.isoformat(timespec="seconds")


@pytest.fixture()
def env(tmp_path):
    """临时库 + 全套仓储 + 固定时间源的清扫服务"""
    db_path, _ = fresh_db(tmp_path, name="index_cleanup.db")
    conn = connect(db_path)
    kb = SQLiteKnowledgeBaseRepository(conn).create(name="测试知识库")
    repos = {
        "documents": SQLiteDocumentRepository(conn),
        "versions": SQLiteDocumentVersionRepository(conn),
        "content": SQLiteContentRepository(conn),
        "indexes": SQLiteIndexVersionRepository(conn),
        "chunks": SQLiteChunkRepository(conn),
        "configs": SQLiteConfigRepository(conn),
        "tasks": SQLiteTaskRepository(conn),
    }
    vector_index = ChromaVectorIndexAdapter(
        # 清扫做全量孤儿扫描：每个测试用独立持久化目录隔离实例，
        # 避免进程内共享内存实例导致跨测试集合互渗
        chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    )
    keyword_index = SQLiteFtsKeywordIndex(conn)
    clock = _FixedClock()
    work_dir = str(tmp_path / "cloud_results")
    service = IndexCleanupService(
        index_repo=repos["indexes"],
        chunk_repo=repos["chunks"],
        task_repo=repos["tasks"],
        vector_index=vector_index,
        keyword_index=keyword_index,
        work_dir=work_dir,
        now_fn=clock,
    )
    yield SimpleNamespace(
        conn=conn,
        repos=repos,
        kb_id=kb.id,
        staging=str(tmp_path / "staging"),
        work_dir=work_dir,
        vector_index=vector_index,
        keyword_index=keyword_index,
        tokenizer=JiebaTokenizer(),
        clock=clock,
        service=service,
    )
    conn.close()


def _backdate_index(env, index_id: str, *, hours: float) -> None:
    """把索引行 created_at 回写到指定小时之前（保留期判定的对齐现场）"""
    backdated = (env.clock.now - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )
    env.conn.execute(
        "UPDATE index_versions SET created_at = ? WHERE id = ?", (backdated, index_id)
    )


def _build_retired_index(env):
    """构建"新索引激活后旧索引退役"的现场（同版本重建切换语义）

    旧索引先激活再被新索引激活顶替为退役；返回 (退役索引, 文档版本)，
    索引实体为落库后回读（创建返回值不含后续登记的集合名）
    """
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    old = _build_index(env, version.id)
    env.repos["indexes"].activate(old.id)
    new = _build_index(env, version.id)
    env.repos["indexes"].activate(new.id)
    assert old.id != new.id
    return env.repos["indexes"].get(old.id), version


def test_retired_index_assets_cleared_row_kept(env):
    """退役索引：派生资产全部清除，索引行保留承载激活历史"""
    retired, _ = _build_retired_index(env)

    report = env.service.sweep()

    assert report.cleaned_counts.get("retired_index") == 1
    assert report.failures == ()
    assert env.repos["indexes"].get(retired.id) is not None
    assert env.vector_index.count_vectors(f"wb-idx-{retired.id}") == 0
    assert env.keyword_index.count_documents(f"fts-{retired.id}") == 0
    assert env.repos["chunks"].count_index_chunks(retired.id) == 0
    # 已清空的退役行不再成为后续目标
    assert env.service.find_targets() == ()


def test_residual_staging_removed_after_retention(env):
    """残留 staging：超过保留期且无在途任务时整行连同资产删除"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)
    _backdate_index(env, index.id, hours=25)

    report = env.service.sweep()

    assert report.cleaned_counts.get("residual_index") == 1
    assert env.repos["indexes"].get(index.id) is None
    assert env.vector_index.count_vectors(f"wb-idx-{index.id}") == 0
    assert env.keyword_index.count_documents(f"fts-{index.id}") == 0
    assert env.repos["chunks"].count_index_chunks(index.id) == 0


def test_residual_staging_within_retention_kept(env):
    """未超保留期的 staging：保留不动"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)

    report = env.service.sweep()

    assert report.found == 0
    assert env.repos["indexes"].get(index.id) is not None
    assert env.vector_index.count_vectors(f"wb-idx-{index.id}") > 0


def test_residual_staging_with_inflight_task_kept(env):
    """超期 staging 但其文档版本存在在途任务：守卫生效保留"""
    version = _stage_version(env, f"{_LONG_PARAGRAPH}\n\n{_LONG_PARAGRAPH}")
    index = _build_index(env, version.id)
    _backdate_index(env, index.id, hours=25)
    env.repos["tasks"].create("import", document_version_id=version.id)

    report = env.service.sweep()

    assert report.found == 0
    assert env.repos["indexes"].get(index.id) is not None


def test_orphan_collection_and_namespace_cleared(env):
    """孤儿集合与命名空间清除；命名不符的资产保持原样"""
    orphan_collection = f"wb-idx-{uuid7()}"
    orphan_namespace = f"fts-{uuid7()}"
    env.vector_index.upsert_vectors(orphan_collection, ["orphan"], [[1.0] * 4])
    env.keyword_index.rebuild_namespace(
        orphan_namespace, [KeywordDocument(chunk_id="c", content="孤儿")]
    )
    env.vector_index.upsert_vectors("legacy-store", ["x"], [[1.0] * 4])
    env.keyword_index.rebuild_namespace(
        "global", [KeywordDocument(chunk_id="y", content="全局")]
    )

    report = env.service.sweep()

    assert report.cleaned_counts.get("orphan_collection") == 1
    assert report.cleaned_counts.get("orphan_namespace") == 1
    assert env.vector_index.count_vectors(orphan_collection) == 0
    assert env.keyword_index.count_documents(orphan_namespace) == 0
    # 命名不符合同的资产不在清扫范围
    assert env.vector_index.count_vectors("legacy-store") == 1
    assert env.keyword_index.count_documents("global") == 1


def test_result_dirs_removed_for_terminal_or_missing_tasks(env):
    """产物目录：终态任务与无任务行的目录删除，在途任务目录保留"""
    terminal_task = env.repos["tasks"].create("import")
    env.repos["tasks"].transition(terminal_task.id, TaskStatus.CANCELLED)
    inflight_task = env.repos["tasks"].create("import")
    for name in (terminal_task.id, inflight_task.id, "unknown-leftover"):
        os.makedirs(os.path.join(env.work_dir, name), exist_ok=True)
        with open(os.path.join(env.work_dir, name, "result.zip"), "wb") as f:
            f.write(b"zip")

    report = env.service.sweep()

    assert report.cleaned_counts.get("result_dir") == 2
    assert os.path.exists(os.path.join(env.work_dir, inflight_task.id))
    assert not os.path.exists(os.path.join(env.work_dir, terminal_task.id))
    assert not os.path.exists(os.path.join(env.work_dir, "unknown-leftover"))


def test_transient_failure_recorded_and_other_targets_cleaned(env):
    """单条目标删除失败：失败记录为瞬态，其余目标照常清理"""
    retired, _ = _build_retired_index(env)
    orphan_namespace = f"fts-{uuid7()}"
    env.keyword_index.rebuild_namespace(
        orphan_namespace, [KeywordDocument(chunk_id="c", content="孤儿")]
    )
    wrapped = BlockedDeleteVectorIndex(env.vector_index, {f"wb-idx-{retired.id}"})
    service = IndexCleanupService(
        index_repo=env.repos["indexes"],
        chunk_repo=env.repos["chunks"],
        task_repo=env.repos["tasks"],
        vector_index=wrapped,
        keyword_index=env.keyword_index,
        work_dir=env.work_dir,
        now_fn=env.clock,
    )

    report = service.sweep()

    assert len(report.failures) == 1
    failure = report.failures[0]
    assert failure.target_id == retired.id
    assert failure.transient is True
    assert report.cleaned_counts.get("orphan_namespace") == 1
    # 失败项资产保留在现场，等待下次清扫重试
    assert env.vector_index.count_vectors(f"wb-idx-{retired.id}") > 0


def test_sweep_is_idempotent(env):
    """清扫重复执行：首轮清空后，次轮不再发现目标"""
    retired, _ = _build_retired_index(env)
    env.vector_index.upsert_vectors(f"wb-idx-{uuid7()}", ["orphan"], [[1.0] * 4])
    os.makedirs(env.work_dir, exist_ok=True)

    first = env.service.sweep()
    assert first.failures == ()
    second = env.service.sweep()
    assert second.found == 0
    assert env.service.find_targets() == ()
    # 退役索引行保留（只清派生资产）
    assert env.repos["indexes"].get(retired.id) is not None
