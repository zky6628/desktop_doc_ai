# -*- coding: utf-8 -*-
"""批量导入编排：暂存校验、路由决策与导入事务的逐文件串联

批次内文件相互独立：任一文件的暂存校验或导入事务失败只产生该
文件的拒绝结果，不影响其他文件。路由决策（模式、理由、配置版本）
序列化后写入任务输入，保证自动决策可追溯。
"""
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.domain import parser_routing
from app.domain.errors import (
    DuplicateActiveContentError,
    EmptyFileError,
    EntityNotFoundError,
    FileTooLargeError,
    InsufficientDiskSpaceError,
    PathUnsafeError,
    TaskQueueFullError,
    UnsupportedFormatError,
)
from app.domain.ports import ImportRepository
from app.infrastructure.storage.upload_staging import UploadStagingStore

# 领域错误到用户可见错误码的映射：key 为异常类型
_STAGING_ERROR_CODES = {
    UnsupportedFormatError: "UNSUPPORTED_FORMAT",
    FileTooLargeError: "FILE_TOO_LARGE",
    EmptyFileError: "EMPTY_FILE",
    InsufficientDiskSpaceError: "INSUFFICIENT_STORAGE",
    PathUnsafeError: "INTERNAL_ERROR",
}

_IMPORT_ERROR_CODES = {
    EntityNotFoundError: "KNOWLEDGE_BASE_NOT_FOUND",
    DuplicateActiveContentError: "DUPLICATE_FILE",
    TaskQueueFullError: "TASK_QUEUE_FULL",
}


@dataclass(frozen=True)
class ImportFileInput:
    """批次内单个待导入文件的输入

    chunks 由调用方按块读取上传流产生（建议 1 MB 块），编排器
    消费即写暂存，不在内存堆积整文件
    """

    display_name: str
    chunks: Iterable[bytes]
    declared_mime: str | None = None
    # 单文件幂等键（可空）：传入时同键重复提交返回既有任务
    idempotency_key: str | None = None


@dataclass(frozen=True)
class FileImportResult:
    """批次内单文件的导入结果（接受或拒绝）"""

    display_name: str
    accepted: bool
    document_id: str | None = None
    document_version_id: str | None = None
    task_id: str | None = None
    task_state: str | None = None
    route_mode: str | None = None
    route_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @staticmethod
    def rejected(display_name: str, error_code: str, error_message: str) -> "FileImportResult":
        """构造拒绝结果"""
        return FileImportResult(
            display_name=display_name,
            accepted=False,
            error_code=error_code,
            error_message=error_message,
        )


class ImportOrchestrator:
    """批量导入编排器

    :param staging_store: 上传暂存器（流式写入与安全校验）
    :param import_repo: 导入仓储（单事务建立文档/版本/任务）
    """

    def __init__(self, staging_store: UploadStagingStore, import_repo: ImportRepository) -> None:
        self._staging = staging_store
        self._imports = import_repo

    def import_batch(
        self,
        kb_id: str,
        files: Sequence[ImportFileInput],
        *,
        duplicate_policy: str = "skip",
        parser_preference: str = "auto",
    ) -> list[FileImportResult]:
        """逐文件执行暂存、路由与导入，返回与输入同序的结果列表

        :param kb_id: 目标知识库 ID
        :param files: 批次文件输入（已按上传顺序排列）
        :param duplicate_policy: 重复内容策略（skip/new_version）
        :param parser_preference: 解析偏好（auto/local/mineru）
        :return: 每个文件的接受/拒绝结果（顺序与输入一致）
        """
        return [
            self._import_one(kb_id, item, duplicate_policy, parser_preference)
            for item in files
        ]

    def _import_one(
        self,
        kb_id: str,
        item: ImportFileInput,
        duplicate_policy: str,
        parser_preference: str,
    ) -> FileImportResult:
        """处理单个文件：暂存 -> 预路由 -> 导入事务

        :param kb_id: 目标知识库 ID
        :param item: 文件输入
        :param duplicate_policy: 重复内容策略
        :param parser_preference: 解析偏好
        :return: 该文件的导入结果
        """
        try:
            staged = self._staging.stage(
                item.chunks, item.display_name, item.declared_mime
            )
        except tuple(_STAGING_ERROR_CODES) as exc:
            return FileImportResult.rejected(
                item.display_name, _STAGING_ERROR_CODES[type(exc)], str(exc)
            )

        route = parser_routing.decide_parser_route(staged.extension, parser_preference)
        try:
            outcome = self._imports.create_import(
                kb_id,
                display_name=staged.display_name,
                source_path=staged.staging_path,
                source_sha256=staged.sha256,
                mime_type=staged.mime_type,
                size_bytes=staged.size_bytes,
                duplicate_policy=duplicate_policy,
                task_type="import",
                parser_mode=route.mode.value,
                parser_route_json=_serialize_route(route, parser_preference),
                idempotency_key=item.idempotency_key,
            )
        except tuple(_IMPORT_ERROR_CODES) as exc:
            return FileImportResult.rejected(
                item.display_name, _IMPORT_ERROR_CODES[type(exc)], str(exc)
            )

        return FileImportResult(
            display_name=item.display_name,
            accepted=True,
            document_id=outcome.document_id,
            document_version_id=outcome.document_version_id,
            task_id=outcome.task.id,
            task_state=outcome.task.state.value,
            route_mode=route.mode.value,
            route_reason=route.reason,
        )


def _serialize_route(
    route: parser_routing.ParserRouteDecision, preference: str
) -> str:
    """把路由决策序列化为任务输入中的可追溯记录

    :param route: 预路由决策
    :param preference: 用户解析偏好
    :return: 紧凑 JSON 文本（不含用户路径与文档内容）
    """
    return json.dumps(
        {
            "mode": route.mode.value,
            "reason": route.reason,
            "router_config_version": route.router_config_version,
            "parser_preference": preference,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
