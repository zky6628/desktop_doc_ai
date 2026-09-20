# -*- coding: utf-8 -*-
"""API v1 文档路由：列表、详情、版本、解析块预览、替换、重建与删除

端点以闭包持有依赖（运行时由应用装配注入，测试可替换），响应统一
走 v1 信封。替换复用导入管线在目标文档上建立新版本（202 返回任务）；
删除为队列长操作（软删除与删除任务同一事务）；重建为已有 worker
分支创建任务入口。文档源路径不出响应（安全合同）。
"""
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse

from app.domain import file_policy
from app.domain.errors import (
    EntityNotFoundError,
    KnowledgeBaseDeletedError,
    TaskQueueFullError,
    TaskStateConflictError,
    VersionConflictError,
)
from app.domain.ports import (
    ContentRepository,
    DeletionRepository,
    DocumentRepository,
    DocumentVersionRepository,
    ImportRepository,
    IndexVersionRepository,
    KnowledgeBaseRepository,
    TaskRepository,
)
from app.infrastructure.ingest import ImportFileInput, ImportOrchestrator

from .envelope import error_envelope, new_request_id, serialize_task, success_envelope
from .files import chunk_file_reader
from .pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor

# 请求流读取块大小与上传入口保持一致
_STREAM_CHUNK_BYTES = file_policy.STREAM_CHUNK_BYTES

# 替换拒绝码到 HTTP 状态码的映射（信封错误码承载语义）
_REPLACE_HTTP_STATUS = {
    "DOCUMENT_NOT_FOUND": 404,
    "KNOWLEDGE_BASE_DELETED": 410,
    "TASK_STATE_CONFLICT": 409,
    "DUPLICATE_FILE": 409,
    "TASK_QUEUE_FULL": 429,
    "UNSUPPORTED_FORMAT": 415,
    "FILE_TOO_LARGE": 413,
    "EMPTY_FILE": 422,
    "INSUFFICIENT_STORAGE": 507,
    "INTERNAL_ERROR": 500,
}


@dataclass(frozen=True)
class DocumentDependencies:
    """文档端点依赖：由应用装配（或测试）构造"""

    orchestrator: ImportOrchestrator
    kb_repo: KnowledgeBaseRepository
    document_repo: DocumentRepository
    version_repo: DocumentVersionRepository
    index_repo: IndexVersionRepository
    content_repo: ContentRepository
    task_repo: TaskRepository
    deletion_repo: DeletionRepository
    import_repo: ImportRepository


def serialize_version(version) -> dict:
    """文档版本实体到响应视图（源路径不出响应）"""
    return {
        "id": version.id,
        "document_id": version.document_id,
        "version_no": version.version_no,
        "source_sha256": version.source_sha256,
        "mime_type": version.mime_type,
        "size_bytes": version.size_bytes,
        "parser_mode": version.parser_mode,
        "parser_provider": version.parser_provider,
        "parser_version": version.parser_version,
        "status": version.status,
        "active_index_version_id": version.active_index_version_id,
        "created_at": version.created_at,
        "activated_at": version.activated_at,
    }


def create_documents_router(deps: DocumentDependencies) -> APIRouter:
    """装配文档路由（随 v1 路由挂载，路径前缀由父路由提供）"""
    router = APIRouter()

    @router.get("/knowledge-bases/{kb_id}/documents")
    def list_documents(
        kb_id: str,
        request: Request,
        cursor: str | None = None,
        limit: int | None = None,
        include_deleted: bool = False,
    ) -> JSONResponse:
        """知识库文档列表（keyset 分页，默认隐藏已软删除）"""
        request_id = new_request_id()
        kb = deps.kb_repo.get(kb_id)
        if kb is None:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    request_id, "KNOWLEDGE_BASE_NOT_FOUND", "知识库不存在"
                ),
            )
        if kb.deleted_at is not None:
            return JSONResponse(
                status_code=410,
                content=error_envelope(
                    request_id, "KNOWLEDGE_BASE_DELETED", "知识库已删除"
                ),
            )
        try:
            page_size = clamp_limit(limit)
            after_created_at, after_id = (
                decode_cursor(cursor) if cursor else (None, None)
            )
        except InvalidCursorError as exc:
            return JSONResponse(
                status_code=400,
                content=error_envelope(request_id, "INVALID_PARAM", str(exc)),
            )
        items = deps.document_repo.list_by_kb(
            kb_id,
            include_deleted=include_deleted,
            limit=page_size,
            after_created_at=after_created_at,
            after_id=after_id,
        )
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id)
            if len(items) == page_size
            else None
        )
        payload = {
            "items": [_document_summary(item) for item in items],
            "next_cursor": next_cursor,
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/documents/{doc_id}")
    def get_document(doc_id: str) -> JSONResponse:
        """文档详情：文档事实、活动版本与最近任务"""
        request_id = new_request_id()
        document = deps.document_repo.get(doc_id)
        if document is None:
            return _document_not_found(request_id)
        active_version = (
            deps.version_repo.get(document.active_document_version_id)
            if document.active_document_version_id
            else None
        )
        recent_tasks = deps.task_repo.list_by_document(doc_id, limit=5)
        payload = {
            "id": document.id,
            "knowledge_base_id": document.knowledge_base_id,
            "display_name": document.display_name,
            "status": document.status.value,
            "deleted_at": document.deleted_at,
            "created_at": document.created_at,
            "updated_at": document.updated_at,
            "active_version": (
                None if active_version is None else serialize_version(active_version)
            ),
            "recent_tasks": [serialize_task(task) for task in recent_tasks],
        }
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/documents/{doc_id}/versions")
    def list_document_versions(doc_id: str) -> JSONResponse:
        """版本历史（版本号升序）"""
        request_id = new_request_id()
        document = deps.document_repo.get(doc_id)
        if document is None:
            return _document_not_found(request_id)
        versions = deps.version_repo.list_by_document(doc_id)
        payload = {"items": [serialize_version(v) for v in versions]}
        return JSONResponse(
            status_code=200, content=success_envelope(payload, request_id)
        )

    @router.get("/documents/{doc_id}/blocks")
    def list_document_blocks(
        doc_id: str, cursor: str | None = None, limit: int | None = None
    ) -> JSONResponse:
        """活动版本的解析块预览（含表格证据；安全渲染由客户端负责）"""
        request_id = new_request_id()
        document = deps.document_repo.get(doc_id)
        if document is None:
            return _document_not_found(request_id)
        if not document.active_document_version_id:
            payload: dict = {
                "active_version_id": None,
                "items": [],
                "next_cursor": None,
            }
            return JSONResponse(
                status_code=200, content=success_envelope(payload, request_id)
            )
        try:
            page_size = clamp_limit(limit)
            after_ordinal = int(cursor) if cursor else None
        except (InvalidCursorError, ValueError):
            return JSONResponse(
                status_code=400,
                content=error_envelope(request_id, "INVALID_PARAM", "分页参数非法"),
            )
        try:
            blocks = deps.content_repo.list_document_blocks(
                document.active_document_version_id
            )
        except EntityNotFoundError:
            return _document_not_found(request_id)
        # 块预览按 ordinal 排序后内存分页（单版本块数量有限）
        page = [
            block
            for block in blocks
            if after_ordinal is None or block.block.ordinal > after_ordinal
        ][:page_size]
        next_cursor = (
            str(page[-1].block.ordinal) if len(page) == page_size else None
        )
        blocks_payload: dict = {
            "active_version_id": document.active_document_version_id,
            "items": [_block_view(block) for block in page],
            "next_cursor": next_cursor,
        }
        return JSONResponse(
            status_code=200, content=success_envelope(blocks_payload, request_id)
        )

    @router.post("/documents/{doc_id}/replace")
    def replace_document(
        doc_id: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        parser_preference: Annotated[str, Form()] = "auto",
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        """替换文档内容：新文件在目标文档上建立新版本（202 + 任务）"""
        request_id = new_request_id()
        result = deps.orchestrator.replace_document(
            doc_id,
            ImportFileInput(
                display_name=file.filename or "",
                declared_mime=file.content_type,
                chunks=chunk_file_reader(file.file, _STREAM_CHUNK_BYTES),
                idempotency_key=idempotency_key,
            ),
            parser_preference=parser_preference,
        )
        if not result.accepted:
            status = _REPLACE_HTTP_STATUS.get(result.error_code or "", 400)
            return JSONResponse(
                status_code=status,
                content=error_envelope(
                    request_id,
                    result.error_code or "UNKNOWN",
                    result.error_message or "替换被拒绝",
                ),
            )
        payload = {
            "result": {
                "display_name": result.display_name,
                "document_id": result.document_id,
                "document_version_id": result.document_version_id,
                "task_id": result.task_id,
                "task_state": result.task_state,
                "route": (
                    None
                    if result.route_mode is None
                    else {"mode": result.route_mode, "reason": result.route_reason}
                ),
            }
        }
        return JSONResponse(
            status_code=202, content=success_envelope(payload, request_id)
        )

    @router.post("/documents/{doc_id}/rebuild")
    def rebuild_document(
        doc_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        """重建文档索引：为当前活动版本创建索引重建任务（202）

        守卫（存在性/活动版本/在途互斥/容量）与任务创建在同一事务内
        裁定，端点不再预检，消除并发启动的竞态窗口
        """
        request_id = new_request_id()
        try:
            task = deps.import_repo.create_rebuild(
                doc_id, idempotency_key=idempotency_key
            )
        except EntityNotFoundError:
            return _document_not_found(request_id)
        except KnowledgeBaseDeletedError as exc:
            return JSONResponse(
                status_code=410,
                content=error_envelope(
                    request_id, "KNOWLEDGE_BASE_DELETED", str(exc)
                ),
            )
        except VersionConflictError as exc:
            return JSONResponse(
                status_code=409,
                content=error_envelope(request_id, "VERSION_CONFLICT", str(exc)),
            )
        except TaskStateConflictError as exc:
            return JSONResponse(
                status_code=409,
                content=error_envelope(request_id, "TASK_STATE_CONFLICT", str(exc)),
            )
        except TaskQueueFullError as exc:
            return JSONResponse(
                status_code=429,
                content=error_envelope(request_id, "TASK_QUEUE_FULL", str(exc)),
            )
        return JSONResponse(
            status_code=202,
            content=success_envelope({"task": serialize_task(task)}, request_id),
        )

    @router.delete("/documents/{doc_id}")
    def delete_document(
        doc_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        """删除文档（软删除 + 物理清理任务，同一事务）：202 返回任务"""
        request_id = new_request_id()
        try:
            task = deps.deletion_repo.create_document_deletion(
                doc_id, idempotency_key=idempotency_key
            )
        except EntityNotFoundError:
            return _document_not_found(request_id)
        except TaskStateConflictError as exc:
            return JSONResponse(
                status_code=409,
                content=error_envelope(request_id, "TASK_STATE_CONFLICT", str(exc)),
            )
        except TaskQueueFullError as exc:
            return JSONResponse(
                status_code=429,
                content=error_envelope(request_id, "TASK_QUEUE_FULL", str(exc)),
            )
        return JSONResponse(
            status_code=202,
            content=success_envelope({"task": serialize_task(task)}, request_id),
        )

    def _document_not_found(request_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=error_envelope(request_id, "DOCUMENT_NOT_FOUND", "文档不存在"),
        )

    def _document_summary(document) -> dict:
        """文档列表项：文档事实 + 活动版本摘要 + 切片数 + 最近任务"""
        active_version = (
            deps.version_repo.get(document.active_document_version_id)
            if document.active_document_version_id
            else None
        )
        chunk_count = None
        if active_version is not None and active_version.active_index_version_id:
            index = deps.index_repo.get(active_version.active_index_version_id)
            chunk_count = index.chunk_count if index is not None else None
        latest = deps.task_repo.list_by_document(document.id, limit=1)
        return {
            "id": document.id,
            "knowledge_base_id": document.knowledge_base_id,
            "display_name": document.display_name,
            "status": document.status.value,
            "deleted_at": document.deleted_at,
            "created_at": document.created_at,
            "updated_at": document.updated_at,
            "active_version": (
                None
                if active_version is None
                else {
                    "id": active_version.id,
                    "version_no": active_version.version_no,
                    "parser_mode": active_version.parser_mode,
                    "status": active_version.status,
                }
            ),
            "chunk_count": chunk_count,
            "latest_task": (
                None if not latest else serialize_task(latest[0])
            ),
        }

    def _block_view(stored) -> dict:
        """解析块预览视图（表格不输出 raw_html，安全渲染由客户端负责）"""
        table = None
        if stored.block.table is not None:
            table = {
                "raw_markdown": stored.block.table.raw_markdown,
                "structure_json": stored.block.table.structure_json,
                "searchable_text": stored.block.table.searchable_text,
            }
        return {
            "id": stored.id,
            "block_type": stored.block.block_type.value,
            "ordinal": stored.block.ordinal,
            "page_no": stored.block.page_no,
            "section_path": stored.block.section_path,
            "content_text": stored.block.text,
            "table": table,
        }

    return router
