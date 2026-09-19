# -*- coding: utf-8 -*-
"""上下文事实解析器测试：真实管线产物到组装输入的全链路"""
from app.infrastructure.retrieval import ContextResolver
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteIndexVersionRepository,
)


def _resolver(env) -> ContextResolver:
    return ContextResolver(
        index_repo=SQLiteIndexVersionRepository(env.conn),
        chunk_repo=SQLiteChunkRepository(env.conn),
        document_repo=SQLiteDocumentRepository(env.conn),
        version_repo=SQLiteDocumentVersionRepository(env.conn),
    )


def test_resolve_returns_pipeline_chunks_with_document_facts(env):
    """真实管线产物可解析：切片事实携带文件名/版本号/父子结构"""
    from tests.infrastructure.schema_helpers import import_file

    task_id = import_file(env, "解析.txt", "苹果是一种水果。\n\n香蕉也是水果。".encode())
    worker = env.build()
    assert worker.process_next() is True
    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"

    sources = _resolver(env).resolve(env.kb_id)

    # 一个父切片 + 一个子切片（两段落合并进同一父切片）
    ordinals = sorted(source.ordinal for source in sources)
    assert ordinals == [0, 1]
    parent = next(source for source in sources if source.parent_ordinal is None)
    child = next(source for source in sources if source.parent_ordinal is not None)
    assert parent.file_name == "解析.txt"
    assert parent.version_no == 1
    assert child.document_version_id == task.document_version_id
    assert child.content in parent.content
    assert child.block_ids


def test_resolve_without_retrievable_indexes_returns_empty(env):
    """无可检索索引返回空列表"""
    assert _resolver(env).resolve(env.kb_id) == []
