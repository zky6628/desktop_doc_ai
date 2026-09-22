# -*- coding: utf-8 -*-
"""导入/维护任务测试共享的 Worker 环境夹具

pytest fixture 无法跨文件导入，需在多个测试文件间共享的夹具按约定
放在本文件。索引健康与清扫测试在各自文件内定义了同名 env 夹具覆盖
本定义，依赖本地特有结构（固定时间源、清扫服务等）。
"""
from types import SimpleNamespace

import chromadb
import pytest

from app.infrastructure.keywordindex import JiebaTokenizer, SQLiteFtsKeywordIndex
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteConfigRepository,
    SQLiteContentRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteEmbeddingCacheRepository,
    SQLiteEvaluationRunRepository,
    SQLiteExternalTaskRepository,
    SQLiteIndexVersionRepository,
    SQLiteKnowledgeBaseRepository,
    SQLiteSystemSettingsRepository,
    SQLiteTaskRepository,
)
from app.infrastructure.sqlite.repositories.import_repository import (
    SQLiteImportRepository,
)
from app.infrastructure.vectorindex import ChromaVectorIndexAdapter
from app.infrastructure.worker import ImportTaskWorker

from .schema_helpers import FakeEmbeddingGateway, fresh_db

# 构造参数哨兵：区分"显式传 None"与"使用夹具默认替身"
_UNSET = object()


@pytest.fixture()
def env(tmp_path):
    """临时库 + 全套仓储 + Worker 构建器（云端客户端由测试注入）

    独立持久化向量目录隔离 Chroma 实例，避免进程内共享内存实例
    导致用例间集合互渗
    """
    db_path, _ = fresh_db(tmp_path, name="worker_env.db")
    conn = connect(db_path)
    kb = SQLiteKnowledgeBaseRepository(conn).create(name="测试知识库")
    repos = {
        "tasks": SQLiteTaskRepository(conn),
        "documents": SQLiteDocumentRepository(conn),
        "versions": SQLiteDocumentVersionRepository(conn),
        "content": SQLiteContentRepository(conn),
        "external": SQLiteExternalTaskRepository(conn),
        "indexes": SQLiteIndexVersionRepository(conn),
        "chunks": SQLiteChunkRepository(conn),
        "configs": SQLiteConfigRepository(conn),
        "settings": SQLiteSystemSettingsRepository(conn),
        "embedding_cache": SQLiteEmbeddingCacheRepository(conn),
        "evaluations": SQLiteEvaluationRunRepository(conn),
        "imports": SQLiteImportRepository(conn),
    }
    vector_adapter = ChromaVectorIndexAdapter(
        chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    )
    keyword_index = SQLiteFtsKeywordIndex(conn)
    fake_gateway = FakeEmbeddingGateway()

    def build(
        mineru=None,
        embedding_gateway=_UNSET,
        vector_index=None,
        evaluation_repo=None,
        evaluation_service=None,
    ):
        return ImportTaskWorker(
            task_repo=repos["tasks"],
            document_repo=repos["documents"],
            version_repo=repos["versions"],
            content_repo=repos["content"],
            external_repo=repos["external"],
            index_repo=repos["indexes"],
            chunk_repo=repos["chunks"],
            config_repo=repos["configs"],
            settings_repo=repos["settings"],
            embedding_cache=repos["embedding_cache"],
            embedding_gateway=(
                fake_gateway if embedding_gateway is _UNSET else embedding_gateway
            ),
            vector_index=(
                vector_adapter if vector_index is None else vector_index
            ),
            text_tokenizer=JiebaTokenizer(),
            keyword_index=keyword_index,
            mineru_client=mineru,
            work_dir=str(tmp_path / "cloud_results"),
            worker_id="worker-test",
            evaluation_repo=evaluation_repo,
            evaluation_service=evaluation_service,
        )

    yield SimpleNamespace(
        conn=conn,
        repos=repos,
        kb_id=kb.id,
        staging=str(tmp_path / "staging"),
        build=build,
        vector_index=vector_adapter,
        keyword_index=keyword_index,
        tokenizer=JiebaTokenizer(),
        gateway=fake_gateway,
    )
    conn.close()
