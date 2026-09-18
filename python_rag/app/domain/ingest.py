# -*- coding: utf-8 -*-
"""导入域对象：批量上传建立文档版本与导入任务时的领域取值

重复内容策略与导入结果在此统一定义，仓储实现与编排器都从这里
取值，避免口径漂移。
"""
from dataclasses import dataclass
from enum import StrEnum

from app.domain.entities import Task

# 用户可见的重复内容策略取值
DUPLICATE_POLICY_VALUES = ("skip", "new_version")


class DuplicatePolicy(StrEnum):
    """知识库内相同内容活动文档的处理策略

    skip：命中重复即拒绝该文件；new_version：复用活动文档并递增版本号
    """

    SKIP = "skip"
    NEW_VERSION = "new_version"


@dataclass(frozen=True)
class ImportOutcome:
    """单文件导入结果：建立（或复用）的文档、新版本与导入任务"""

    document_id: str
    document_version_id: str
    task: Task
    # 命中重复内容且策略为 new_version 时复用既有活动文档，此时为 True
    reused_existing_document: bool
