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

import pytest
from docx import Document as DocxDocument

from app.domain.entities import TaskStage, TaskStatus
from app.domain.errors import CloudTransportError
from app.domain.parser_routing import decide_parser_route
from app.infrastructure.mineru.archive import extract_archive
from app.infrastructure.mineru.dto import (
    BatchPollResult,
    BatchSubmission,
    ProviderFileStatus,
)
from app.infrastructure.parsing import TxtMarkdownParser
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteContentRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteExternalTaskRepository,
    SQLiteKnowledgeBaseRepository,
    SQLiteTaskRepository,
)
from app.infrastructure.sqlite.repositories.import_repository import (
    SQLiteImportRepository,
)
from app.infrastructure.storage.upload_staging import UploadStagingStore
from app.infrastructure.worker import ImportTaskWorker

from .schema_helpers import fresh_db
from .test_parsing_pdf import _build_pdf


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
        "imports": SQLiteImportRepository(conn),
    }

    def build(mineru=None) -> ImportTaskWorker:
        return ImportTaskWorker(
            task_repo=repos["tasks"],
            document_repo=repos["documents"],
            version_repo=repos["versions"],
            content_repo=repos["content"],
            external_repo=repos["external"],
            mineru_client=mineru,
            work_dir=str(tmp_path / "cloud_results"),
            worker_id="worker-test",
        )

    yield SimpleNamespace(
        conn=conn, repos=repos, staging=str(tmp_path / "staging"),
        kb_id=kb.id, build=build,
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

    # 文档状态归索引激活里程碑接线，本次保持导入时的取值
    document = env.repos["documents"].get(task.document_id)
    assert document.status.value == "queued"


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
