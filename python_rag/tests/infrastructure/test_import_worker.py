# -*- coding: utf-8 -*-
"""导入任务 Worker 测试：本地路线、云端路线、重试/取消/恢复与落库幂等

云端路线使用客户端替身（真实 zip 下载与解压、零网络）验证完整编排；
本地路线使用真实解析器与真实暂存文件验证端到端落库。
"""
import hashlib
import io
import json
import os
import time
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import chromadb
import pytest
from docx import Document as DocxDocument

from app.domain.chunking import chunk_blocks
from app.domain.entities import TaskStage, TaskStatus
from app.domain.errors import CloudTransportError, EmbeddingTransientError
from app.domain.parser_routing import decide_parser_route
from app.infrastructure.keywordindex import JiebaTokenizer, SQLiteFtsKeywordIndex
from app.infrastructure.mineru.archive import extract_archive
from app.infrastructure.mineru.dto import (
    BatchPollResult,
    BatchSubmission,
    ProviderFileStatus,
)
from app.infrastructure.parsing import TxtMarkdownParser
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteConfigRepository,
    SQLiteContentRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteExternalTaskRepository,
    SQLiteIndexVersionRepository,
    SQLiteKnowledgeBaseRepository,
    SQLiteTaskRepository,
)
from app.infrastructure.sqlite.repositories.import_repository import (
    SQLiteImportRepository,
)
from app.infrastructure.storage.upload_staging import UploadStagingStore
from app.infrastructure.vectorindex import ChromaVectorIndexAdapter
from app.infrastructure.worker import ImportTaskWorker

from .schema_helpers import fresh_db
from .test_parsing_pdf import _build_pdf

# 构造参数哨兵：区分"显式传 None"与"使用夹具默认替身"
_UNSET = object()


class _FakeEmbeddingGateway:
    """确定性向量化替身：记录调用文本，可选首调挂钩（取消场景用）"""

    def __init__(self, on_first_call=None) -> None:
        self.embedded_texts: list[str] = []
        self._on_first_call = on_first_call

    def embed_texts(self, texts):
        if self._on_first_call is not None:
            hook, self._on_first_call = self._on_first_call, None
            hook()
        self.embedded_texts.extend(texts)
        return [[float(len(text) % 9 + 1)] * 4 for text in texts]


@pytest.fixture()
def env(tmp_path):
    """临时库 + 全套仓储 + Worker 构建器（云端客户端由测试注入）"""
    db_path, _ = fresh_db(tmp_path, name="import_worker.db")
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
        "imports": SQLiteImportRepository(conn),
    }

    def build(mineru=None, embedding_gateway=_UNSET, vector_index=None):
        return ImportTaskWorker(
            task_repo=repos["tasks"],
            document_repo=repos["documents"],
            version_repo=repos["versions"],
            content_repo=repos["content"],
            external_repo=repos["external"],
            index_repo=repos["indexes"],
            chunk_repo=repos["chunks"],
            config_repo=repos["configs"],
            embedding_gateway=(
                fake_gateway if embedding_gateway is _UNSET else embedding_gateway
            ),
            vector_index=(
                vector_adapter if vector_index is None else vector_index
            ),
            text_tokenizer=JiebaTokenizer(),
            keyword_index=SQLiteFtsKeywordIndex(conn),
            mineru_client=mineru,
            work_dir=str(tmp_path / "cloud_results"),
            worker_id="worker-test",
        )

    chroma_client = chromadb.EphemeralClient()
    vector_adapter = ChromaVectorIndexAdapter(chroma_client)
    fake_gateway = _FakeEmbeddingGateway()

    yield SimpleNamespace(
        conn=conn, repos=repos, staging=str(tmp_path / "staging"),
        kb_id=kb.id, build=build, vector_index=vector_adapter,
        gateway=fake_gateway,
    )
    conn.close()


def _import_file(env, filename: str, content: bytes, preference: str = "auto") -> str:
    """经真实暂存与导入事务建立导入任务，返回任务 ID"""
    staged = UploadStagingStore(env.staging).stage(iter([content]), filename)
    route = decide_parser_route(staged.extension, preference)
    outcome = env.repos["imports"].create_import(
        env.kb_id,
        display_name=staged.display_name,
        source_path=staged.staging_path,
        source_sha256=staged.sha256,
        mime_type=staged.mime_type,
        size_bytes=staged.size_bytes,
        parser_mode=route.mode.value,
        parser_route_json=json.dumps(
            {
                "mode": route.mode.value,
                "reason": route.reason,
                "router_config_version": route.router_config_version,
                "parser_preference": preference,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    return outcome.task.id


def _block_types(env, version_id: str) -> list[str]:
    rows = env.conn.execute(
        "SELECT block_type FROM content_blocks"
        " WHERE document_version_id = ? ORDER BY ordinal",
        (version_id,),
    ).fetchall()
    return [row[0] for row in rows]


class _FakeMinerU:
    """云端解析客户端替身：轮询脚本可控，下载产出真实 zip，零网络"""

    def __init__(
        self,
        markdown_text: str,
        *,
        poll_script: list[str] | None = None,
        transient_failures: int = 0,
        provider_error: str | None = None,
        on_first_poll: Callable[[], None] | None = None,
    ) -> None:
        self.markdown_text = markdown_text
        self.poll_script = poll_script or ["done"]
        self.transient_failures = transient_failures
        self.provider_error = provider_error
        self.on_first_poll = on_first_poll
        self.create_batch_calls = 0
        self.uploaded_paths: list[str] = []
        self.poll_calls = 0
        self.source_ref = ""

    def create_batch(self, entries):
        self.create_batch_calls += 1
        return BatchSubmission(
            batch_id="batch-1",
            upload_urls=("https://mock.example/upload",),
            upload_url_expires_at="2026-12-31T00:00:00+00:00",
        )

    def upload_file(self, upload_url, file_path):
        self.uploaded_paths.append(file_path)

    def poll_batch(self, batch_id):
        self.poll_calls += 1
        if self.poll_calls == 1 and self.on_first_poll is not None:
            self.on_first_poll()
        if self.poll_calls <= self.transient_failures:
            raise CloudTransportError("轮询网络失败")
        state = self.poll_script[min(self.poll_calls - 1, len(self.poll_script) - 1)]
        if state == "failed":
            return BatchPollResult(
                batch_id=batch_id,
                files=(
                    ProviderFileStatus(
                        source_ref=self.source_ref,
                        state="failed",
                        err_msg=self.provider_error or "解析失败，请稍后再试",
                    ),
                ),
            )
        return BatchPollResult(
            batch_id=batch_id,
            files=(
                ProviderFileStatus(
                    source_ref=self.source_ref,
                    state=state,
                    full_zip_url=(
                        "https://mock.example/res/1" if state == "done" else None
                    ),
                ),
            ),
        )

    def download_result(self, result_url, dest_path, **kwargs):
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with zipfile.ZipFile(dest_path, "w") as archive:
            archive.writestr("full.md", self.markdown_text)
        with open(dest_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def extract_result_archive(self, zip_path, dest_dir):
        return extract_archive(zip_path, dest_dir)


# ===================== 本地路线 =====================

def test_local_txt_processed_to_succeeded(env):
    """TXT 任务自动跑完：内容落库、版本回写、任务成功、文档状态不动"""
    task_id = _import_file(env, "笔记.txt", "第一段内容\n\n第二段内容".encode())
    worker = env.build()

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    assert task.stage.value == "completed"
    assert task.progress == 1.0

    version_id = task.document_version_id
    version = env.repos["versions"].get(version_id)
    assert version.status == "parsed"
    assert _block_types(env, version_id) == ["paragraph", "paragraph"]

    # 解析内容哈希与解析器对同输入复算的结构哈希一致（确定性）
    expected = TxtMarkdownParser(markdown_mode=False).parse(
        version.source_path, version_id
    ).structure_sha256
    assert version.parsed_content_sha256 == expected
    assert version.parser_provider == "local"

    # 索引激活后文档级指针回填并置 ready
    document = env.repos["documents"].get(task.document_id)
    assert document.status.value == "ready"


def test_local_docx_table_evidence_persisted(env):
    """DOCX 表格块落库且表格证据随块写入"""
    buffer = io.BytesIO()
    document = DocxDocument()
    document.add_heading("表", level=1)
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "甲"
    table.cell(0, 1).text = "乙"
    document.save(buffer)

    task_id = _import_file(env, "表.docx", buffer.getvalue())
    worker = env.build()

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    assert _block_types(env, task.document_version_id) == ["heading", "table"]
    searchable = env.conn.execute(
        "SELECT searchable_text FROM tables"
        " WHERE block_id IN (SELECT id FROM content_blocks"
        "  WHERE document_version_id = ?)",
        (task.document_version_id,),
    ).fetchall()
    assert any("甲" in row[0] for row in searchable)


def test_blank_content_fails_with_parsing_code(env):
    """全空白内容的任务转入失败终态并携带解析错误码"""
    task_id = _import_file(env, "空白.txt", b"  \n\t ")
    worker = env.build()

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "failed"
    assert task.error_code == "PARSING_EMPTY_CONTENT"
    assert task.finished_at is not None


def test_scan_pdf_waits_for_user_then_approval_routes_cloud(env):
    """扫描件信号：任务转入等待用户确认；批准后回排队进入云端提交阶段"""
    task_id = _import_file(env, "扫描件.pdf", _build_pdf([[]]))
    worker = env.build()

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "waiting_user"
    assert task.stage.value == "routing_parser"
    # 扫描件无文本层，不落任何内容块
    assert _block_types(env, task.document_version_id) == []

    from app.infrastructure.ingest import confirm_cloud_parsing

    confirmed = confirm_cloud_parsing(env.repos["tasks"], task_id, "approve")
    assert confirmed.state.value == "queued"
    assert confirmed.stage.value == "submitting_cloud"


def test_scan_pdf_rejection_cancels_task(env):
    """用户拒绝云端解析后任务直接取消收尾"""
    task_id = _import_file(env, "扫描件.pdf", _build_pdf([[]]))
    worker = env.build()
    worker.process_next()

    from app.infrastructure.ingest import confirm_cloud_parsing

    confirmed = confirm_cloud_parsing(env.repos["tasks"], task_id, "reject")
    assert confirmed.state.value == "cancelled"


def test_local_task_activates_index_and_document(env):
    """任务成功后索引原子激活：向量集合、验证基准与文档指针全部落位"""
    long_text = "字" * 500
    task_id = _import_file(
        env, "笔记.txt", (long_text + "\n\n" + long_text).encode()
    )
    worker = env.build()

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    version_id = task.document_version_id
    indexes = env.repos["indexes"].list_by_document_version(version_id)
    assert len(indexes) == 1
    assert indexes[0].status.value == "active"
    assert indexes[0].vector_collection == f"wb-idx-{indexes[0].id}"
    # 切片与嵌入配置均登记为流水线配置行
    assert indexes[0].chunking_config_id is not None
    assert indexes[0].embedding_profile_id is not None

    # 向量集合与子切片一一对应，关键词索引同步落库，验证基准已记录
    child_count = env.conn.execute(
        "SELECT COUNT(*) FROM chunks"
        " WHERE index_version_id = ? AND parent_chunk_id IS NOT NULL",
        (indexes[0].id,),
    ).fetchone()[0]
    assert indexes[0].chunk_count == child_count
    assert indexes[0].integrity_hash
    assert env.vector_index.count_vectors(indexes[0].vector_collection) == child_count
    fts_rows = env.conn.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE fts_namespace = ?",
        (indexes[0].fts_namespace,),
    ).fetchone()[0]
    assert fts_rows == child_count
    assert indexes[0].fts_namespace == f"fts-{indexes[0].id}"
    keyword_config_id = env.conn.execute(
        "SELECT keyword_config_id FROM index_versions WHERE id = ?",
        (indexes[0].id,),
    ).fetchone()[0]
    keyword_config = env.conn.execute(
        "SELECT config_type FROM pipeline_configs WHERE id = ?",
        (keyword_config_id,),
    ).fetchone()
    assert keyword_config[0] == "keyword"

    # 激活回填文档级指针并置 ready
    document = env.repos["documents"].get(task.document_id)
    assert document.status.value == "ready"
    assert document.active_document_version_id == version_id


def _ensure_configs(env) -> tuple[str, str]:
    """确保切片与嵌入配置行存在（构造中断现场用），返回（切片, 嵌入）配置 ID"""
    chunk_config_id = env.repos["configs"].ensure_config(
        "chunking",
        json.dumps(
            {
                "chunking_config_version": "1",
                "parent_chunk_chars": 1200,
                "child_chunk_chars": 400,
                "content_separator": "\n\n",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    embed_config_id = env.repos["configs"].ensure_config(
        "embedding",
        json.dumps(
            {
                "embedding_config_version": "1",
                "model": "text-embedding-v4",
                "dimensions": 1024,
                "batch_size": 10,
                "text_type": "document",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return chunk_config_id, embed_config_id


def _backdate_lease(env, task_id: str, stage: str) -> None:
    """把任务置入指定阶段并使其租约失效（模拟 Worker 崩溃现场）"""
    expires_at = (
        datetime.now(timezone.utc) - timedelta(seconds=31)
    ).isoformat(timespec="seconds")
    env.conn.execute(
        "UPDATE tasks SET stage = ?, lease_expires_at = ? WHERE id = ?",
        (stage, expires_at, task_id),
    )


def _write_parse_facts(env, version_id: str) -> None:
    """把暂存文件的解析事实落库（向量/切片中断必然发生在解析完成之后）"""
    version = env.repos["versions"].get(version_id)
    parsed = TxtMarkdownParser(markdown_mode=False).parse(version.source_path, version_id)
    env.repos["content"].replace_document_content(version_id, parsed)


def _recovered(env) -> None:
    assert env.repos["tasks"].recover_interrupted_tasks()[
        "stale_running_requeued"
    ] == 1


def test_chunking_resume_reuses_staging_index(env):
    """切片中断续跑：复用既有 staging 索引，切片无重复"""
    task_id = _import_file(env, "笔记.txt", "切片内容\n\n更多内容".encode())
    worker = env.build()

    # 构造"切片阶段崩溃"现场：任务停留在 chunking 阶段且租约失效，
    # 前序尝试已确保配置并创建 staging 索引
    claimed = env.repos["tasks"].claim_next("worker-crashed", task_type="import")
    assert claimed.id == task_id
    chunk_config_id, embed_config_id = _ensure_configs(env)
    index_version = env.repos["indexes"].create(
        claimed.document_version_id,
        chunking_config_id=chunk_config_id,
        embedding_profile_id=embed_config_id,
    )
    _write_parse_facts(env, claimed.document_version_id)
    _backdate_lease(env, task_id, "chunking")
    _recovered(env)

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    # staging 索引被复用而非新建，最终完成激活
    indexes = env.repos["indexes"].list_by_document_version(
        claimed.document_version_id
    )
    assert len(indexes) == 1
    assert indexes[0].id == index_version.id
    assert indexes[0].status.value == "active"
    ordinals = [
        row[0]
        for row in env.conn.execute(
            "SELECT ordinal FROM chunks WHERE index_version_id = ? ORDER BY ordinal",
            (index_version.id,),
        ).fetchall()
    ]
    assert ordinals == list(range(len(ordinals)))


def test_embedding_resume_continues_from_cursor(env):
    """嵌入中断续跑：从游标批次继续，已完成批不重复向量化"""
    long_text = "字" * 500
    task_id = _import_file(env, "笔记.txt", (long_text + "\n\n" + long_text).encode())
    worker = env.build()

    # 构造"第一批嵌入完成"现场：切片已落库、首批子切片向量已写入、
    # 游标推进到首批末尾序号、租约失效
    claimed = env.repos["tasks"].claim_next("worker-crashed", task_type="import")
    assert claimed.id == task_id
    chunk_config_id, embed_config_id = _ensure_configs(env)
    index_version = env.repos["indexes"].create(
        claimed.document_version_id,
        chunking_config_id=chunk_config_id,
        embedding_profile_id=embed_config_id,
    )
    _write_parse_facts(env, claimed.document_version_id)
    env.repos["chunks"].replace_index_chunks(
        index_version.id,
        chunk_blocks(
            env.repos["content"].list_document_blocks(claimed.document_version_id)
        ),
    )
    stored_chunks = env.repos["chunks"].list_index_chunks(index_version.id)
    children = [s for s in stored_chunks if s.chunk.parent_ordinal is not None]
    assert len(children) >= 2
    first_batch = children[:10]
    env.vector_index.upsert_vectors(
        f"wb-idx-{index_version.id}",
        [stored.id for stored in first_batch],
        [[1.0] * 4] * len(first_batch),
    )
    fresh_gateway = _FakeEmbeddingGateway()
    worker = env.build(embedding_gateway=fresh_gateway)
    _backdate_lease(env, task_id, "embedding")
    env.conn.execute(
        "UPDATE tasks SET checkpoint_json = ? WHERE id = ?",
        (json.dumps({"embedded_until": first_batch[-1].chunk.ordinal}), task_id),
    )
    _recovered(env)

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    # 仅游标之后的子切片被向量化
    expected_tail = [s.chunk.content for s in children[10:]]
    assert fresh_gateway.embedded_texts == expected_tail
    # 全量子切片最终齐备，任务完成激活
    assert env.vector_index.count_vectors(f"wb-idx-{index_version.id}") == len(children)


def test_transient_embedding_error_retries_then_succeeds(env):
    """瞬态嵌入失败走自动重试；到期提升后续跑到激活成功"""
    task_id = _import_file(env, "笔记.txt", "可重试内容".encode())

    class _FlakyGateway(_FakeEmbeddingGateway):
        """首批抛瞬态错误，其后正常"""

        def embed_texts(self, texts):
            if self.embedded_texts or not texts:
                return super().embed_texts(texts)
            self.embedded_texts.append("__failed__")
            raise EmbeddingTransientError("供应商限流")

    worker = env.build(embedding_gateway=_FlakyGateway())

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "retry_waiting"
    assert task.error_code == "EMBEDDING_TRANSIENT"

    past = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat(timespec="seconds")
    env.conn.execute(
        "UPDATE tasks SET next_retry_at = ? WHERE id = ?", (past, task_id)
    )
    assert env.repos["tasks"].promote_due_retries() == 1

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    indexes = env.repos["indexes"].list_by_document_version(task.document_version_id)
    assert indexes[0].status.value == "active"


def test_missing_gateway_fails_with_auth_code(env):
    """未配置向量化网关：嵌入阶段按认证失败处理，索引保持 staging"""
    task_id = _import_file(env, "笔记.txt", "内容".encode())
    worker = env.build(embedding_gateway=None)

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "failed"
    assert task.error_code == "EMBEDDING_AUTH"
    indexes = env.repos["indexes"].list_by_document_version(task.document_version_id)
    assert indexes[0].status.value == "staging"
    document = env.repos["documents"].get(task.document_id)
    assert document.status.value == "queued"


def test_validation_mismatch_fails_without_activation(env):
    """向量集合残留异物导致校验失败：任务失败且不激活任何索引"""
    task_id = _import_file(env, "笔记.txt", "校验内容".encode())
    worker = env.build()

    # 构造"集合存在残留向量"现场（此前中断尝试的遗留）
    claimed = env.repos["tasks"].claim_next("worker-crashed", task_type="import")
    assert claimed.id == task_id
    chunk_config_id, embed_config_id = _ensure_configs(env)
    index_version = env.repos["indexes"].create(
        claimed.document_version_id,
        chunking_config_id=chunk_config_id,
        embedding_profile_id=embed_config_id,
    )
    _write_parse_facts(env, claimed.document_version_id)
    env.repos["chunks"].replace_index_chunks(
        index_version.id,
        chunk_blocks(
            env.repos["content"].list_document_blocks(claimed.document_version_id)
        ),
    )
    env.vector_index.upsert_vectors(
        f"wb-idx-{index_version.id}", ["alien-residual"], [[9.9] * 4]
    )
    _backdate_lease(env, task_id, "embedding")
    _recovered(env)

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "failed"
    assert task.error_code == "INDEX_VALIDATION_FAILED"
    # 失败不产生任何激活，文档指针保持为空
    indexes = env.repos["indexes"].list_by_document_version(claimed.document_version_id)
    assert all(index.status.value == "staging" for index in indexes)
    document = env.repos["documents"].get(claimed.document_id)
    assert document.active_document_version_id is None


def test_cancel_during_embedding_leaves_staging(env):
    """嵌入批次间收到取消：任务取消收尾，不产生激活"""
    task_id = _import_file(env, "笔记.txt", "取消场景内容".encode())
    gateway = _FakeEmbeddingGateway(
        on_first_call=lambda: env.repos["tasks"].request_cancel(task_id)
    )
    worker = env.build(embedding_gateway=gateway)

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "cancelled"
    indexes = env.repos["indexes"].list_by_document_version(task.document_version_id)
    assert all(index.status.value != "active" for index in indexes)
    document = env.repos["documents"].get(task.document_id)
    assert document.status.value != "ready"


# ===================== 云端路线 =====================

def test_cloud_pipeline_to_succeeded(env):
    """云端任务全流程：提交上传登记 -> 轮询终态转排队 -> 下载归一化落库成功

    等待外部结果是领取内的过渡态（轮询在本次领取内联执行），
    领取结束时任务已按轮询结果转回排队等待下载阶段
    """
    task_id = _import_file(
        env, "云文档.md", "# 标题\n\n正文内容".encode(), preference="mineru"
    )
    version_id = env.repos["tasks"].get(task_id).document_version_id
    fake = _FakeMinerU("# 标题\n\n正文内容")
    fake.source_ref = version_id
    worker = env.build(mineru=fake)

    # 第一轮领取：提交并上传，登记批次后轮询至终态并转回排队
    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "queued"
    assert task.stage.value == "downloading_cloud_result"
    assert fake.create_batch_calls == 1
    assert len(fake.uploaded_paths) == 1

    external = env.repos["external"].list_by_task(task_id)[0]
    assert external.provider_batch_ref == "batch-1"
    assert external.source_ref == version_id
    assert external.poll_count >= 1
    assert external.state == "done"

    # 第二轮领取：下载解压归一化后成功落库
    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    assert task.stage.value == "completed"
    assert _block_types(env, version_id) == ["heading", "paragraph"]
    external = env.repos["external"].list_by_task(task_id)[0]
    assert external.result_sha256

    version = env.repos["versions"].get(version_id)
    assert version.status == "parsed"
    assert version.parser_provider == "mineru"


def test_recovered_task_resumes_without_resubmit(env):
    """等待外部结果中断后恢复：以既有批次续跑轮询，不重复提交批次"""
    task_id = _import_file(
        env, "云文档.md", "内容".encode(), preference="mineru"
    )
    version_id = env.repos["tasks"].get(task_id).document_version_id
    fake = _FakeMinerU("内容")
    fake.source_ref = version_id
    worker = env.build(mineru=fake)

    # 构造"前序 Worker 提交批次后、轮询完成前崩溃"的现场：
    # 批次已登记、任务处于等待外部结果且租约已失效
    claimed = env.repos["tasks"].claim_next("worker-crashed", task_type="import")
    assert claimed.id == task_id
    env.repos["external"].register(
        task_id=task_id,
        provider="mineru",
        provider_batch_ref="batch-1",
        source_ref=version_id,
    )
    env.repos["tasks"].transition(
        task_id, TaskStatus.WAITING_EXTERNAL, stage=TaskStage.POLLING_CLOUD
    )
    expires_at = (
        datetime.now(timezone.utc) - timedelta(seconds=31)
    ).isoformat(timespec="seconds")
    env.conn.execute(
        "UPDATE tasks SET lease_expires_at = ? WHERE id = ?", (expires_at, task_id)
    )
    assert env.repos["tasks"].recover_interrupted_tasks()[
        "stale_running_requeued"
    ] == 1

    # 恢复后续跑：轮询既有批次 -> 下载落库，全程不重新提交批次
    assert worker.process_next() is True
    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    assert fake.create_batch_calls == 0


def test_transient_poll_error_retries_then_recovers(env):
    """瞬态轮询错误安排自动重试；到期提升后续跑至成功"""
    task_id = _import_file(
        env, "云文档.md", "内容".encode(), preference="mineru"
    )
    version_id = env.repos["tasks"].get(task_id).document_version_id
    fake = _FakeMinerU("内容", transient_failures=1)
    fake.source_ref = version_id
    worker = env.build(mineru=fake)

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "retry_waiting"
    assert task.error_code == "CLOUD_TRANSIENT"

    # 退避到期：提升回排队后重新领取续跑
    past = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat(timespec="seconds")
    env.conn.execute(
        "UPDATE tasks SET next_retry_at = ? WHERE id = ?", (past, task_id)
    )
    assert env.repos["tasks"].promote_due_retries() == 1

    assert worker.process_next() is True
    assert worker.process_next() is True

    assert env.repos["tasks"].get(task_id).state.value == "succeeded"


def test_provider_failed_file_fails_task(env):
    """供应方对单文件的失败结果：任务转入失败终态并携带原因文本"""
    task_id = _import_file(
        env, "云文档.md", "内容".encode(), preference="mineru"
    )
    version_id = env.repos["tasks"].get(task_id).document_version_id
    fake = _FakeMinerU(
        "内容", poll_script=["failed"], provider_error="文件页数超出限制"
    )
    fake.source_ref = version_id
    worker = env.build(mineru=fake)

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "failed"
    assert task.error_code == "CLOUD_PARSING_FAILED"
    assert "文件页数超出限制" in task.error_message


def test_cloud_without_token_fails_with_auth_code(env):
    """未配置云端令牌：云端任务在提交时按认证失败处理"""
    task_id = _import_file(
        env, "云文档.md", "内容".encode(), preference="mineru"
    )
    worker = env.build(mineru=None)

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "failed"
    assert task.error_code == "CLOUD_AUTH"


def test_cancel_during_polling_finishes_at_checkpoint(env):
    """轮询中收到取消请求：在轮询检查点收尾为取消终态"""
    task_id = _import_file(
        env, "云文档.md", "内容".encode(), preference="mineru"
    )
    version_id = env.repos["tasks"].get(task_id).document_version_id
    fake = _FakeMinerU(
        "内容",
        poll_script=["running", "done"],
        on_first_poll=lambda: env.repos["tasks"].request_cancel(task_id),
    )
    fake.source_ref = version_id
    worker = env.build(mineru=fake)

    assert worker.process_next() is True

    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "cancelled"
    assert task.finished_at is not None


def test_corrupt_route_input_fails_task(env):
    """任务输入的路由决策被破坏：按内部错误转入失败终态"""
    task = env.repos["tasks"].create("import", input_json="not-json")
    worker = env.build()

    assert worker.process_next() is True

    failed = env.repos["tasks"].get(task.id)
    assert failed.state.value == "failed"
    assert failed.error_code == "INTERNAL_ERROR"


# ===================== 主循环 =====================

def test_worker_loop_drains_queue_and_stops(env):
    """后台循环消费排队任务至成功，停止请求后线程退出"""
    task_a = _import_file(env, "a.txt", "甲的内容".encode())
    task_b = _import_file(env, "b.txt", "乙的内容".encode())
    worker = env.build()

    thread = worker.start_background()
    deadline = time.time() + 10
    states = []
    while time.time() < deadline:
        states = [
            env.repos["tasks"].get(task_id).state.value
            for task_id in (task_a, task_b)
        ]
        if states == ["succeeded", "succeeded"]:
            break
        time.sleep(0.05)

    assert states == ["succeeded", "succeeded"]

    worker.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
