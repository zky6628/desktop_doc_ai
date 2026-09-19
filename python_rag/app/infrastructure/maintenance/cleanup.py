# -*- coding: utf-8 -*-
"""派生索引补偿清理服务

清扫语义为全量幂等：每次执行重新发现全部可清理目标并逐条处理，
单条失败记录后跳过继续，已清理项不回滚。目标只接受从数据库事实
解析出的受控资源（索引行、按命名合同解析的集合与命名空间、任务
目录），不接受任意客户端输入：

- 退役索引：立即清理派生资产（向量集合、FTS 命名空间、切片行），
  索引行保留承载激活历史与验证基准；资产已不存在的行不再成为目标
- 残留索引（staging/validating/failed）：超过保留期限且其文档版本
  无在途任务时，整行连同派生资产一并删除
- 孤儿集合与命名空间：按命名合同前缀解析且无任何索引行引用
- 云端产物目录：任务已终态或不存在对应任务行的工作目录

删除顺序先外部资产后索引行：中途崩溃只留下可重试的残留行，不会
产生无记录的孤儿资产。
"""
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.clock import utc_now_iso
from app.domain.entities import IndexVersion, IndexVersionStatus
from app.domain.errors import PathUnsafeError
from app.domain.index_maintenance import (
    COLLECTION_PREFIX,
    FTS_NAMESPACE_PREFIX,
    collection_name,
    fts_namespace_name,
)
from app.domain.ports import (
    ChunkRepository,
    IndexVersionRepository,
    KeywordIndexGateway,
    TaskRepository,
    VectorIndexGateway,
)
from app.domain.task_state import TERMINAL_STATES

# 失败/取消 staging 的最长保留期：到期后才允许清扫
RESIDUE_MAX_AGE = timedelta(hours=24)

# 清理目标类别
TARGET_RETIRED_INDEX = "retired_index"
TARGET_RESIDUAL_INDEX = "residual_index"
TARGET_ORPHAN_COLLECTION = "orphan_collection"
TARGET_ORPHAN_NAMESPACE = "orphan_namespace"
TARGET_RESULT_DIR = "result_dir"


@dataclass(frozen=True)
class CleanupTarget:
    """单个可清理目标：类别 + 受控资源标识

    target_id 按类别分别为索引版本 ID、集合名、命名空间或任务目录名
    """

    kind: str
    target_id: str


@dataclass(frozen=True)
class CleanupFailure:
    """单条清理失败记录（原因含异常类型名，供事件审计与重试判定）

    transient 标记文件系统类瞬态失败（目录被占用等）：全部失败项
    均为瞬态时清扫任务走自动重试，否则转失败终态由补偿任务接续
    """

    kind: str
    target_id: str
    reason: str
    transient: bool


@dataclass(frozen=True)
class CleanupReport:
    """一次清扫的执行报告"""

    found: int
    cleaned_counts: dict[str, int] = field(default_factory=dict)
    failures: tuple[CleanupFailure, ...] = ()

    @property
    def cleaned(self) -> int:
        """成功清理的目标总数"""
        return sum(self.cleaned_counts.values())

    def summary_json(self) -> str:
        """产出审计事件用的脱敏摘要（不含路径与文档内容）"""
        return json.dumps(
            {
                "found": self.found,
                "cleaned": dict(sorted(self.cleaned_counts.items())),
                "failed": [
                    {
                        "kind": failure.kind,
                        "id": failure.target_id,
                        "reason": failure.reason,
                        "transient": failure.transient,
                    }
                    for failure in self.failures
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


class IndexCleanupService:
    """派生索引补偿清理

    :param index_repo: 索引版本仓储（清理目标与资产归属的事实源）
    :param chunk_repo: 切片仓储（切片行清理与残留判定）
    :param task_repo: 任务仓储（在途任务守卫与目录归属判定）
    :param vector_index: 向量索引网关（集合删除与孤儿扫描）
    :param keyword_index: 关键词索引网关（命名空间清空与孤儿扫描）
    :param work_dir: 云端产物受控工作目录（其下按任务 ID 分目录）
    :param now_fn: 当前时间源（UTC ISO-8601 文本），测试可注入
    """

    def __init__(
        self,
        *,
        index_repo: IndexVersionRepository,
        chunk_repo: ChunkRepository,
        task_repo: TaskRepository,
        vector_index: VectorIndexGateway,
        keyword_index: KeywordIndexGateway,
        work_dir: str,
        now_fn: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._index_repo = index_repo
        self._chunk_repo = chunk_repo
        self._task_repo = task_repo
        self._vector_index = vector_index
        self._keyword_index = keyword_index
        self._work_dir = work_dir
        self._now_fn = now_fn

    def find_targets(self) -> tuple[CleanupTarget, ...]:
        """只读发现全部可清理目标（周期兜底扫描用，不产生副作用）"""
        return tuple(self._discover())

    def sweep(self) -> CleanupReport:
        """执行一次全量清扫，返回审计报告

        单条失败不中断清扫：失败项留在现场，下一次清扫自动重试
        """
        targets = self._discover()
        cleaned_counts: dict[str, int] = {}
        failures: list[CleanupFailure] = []
        for target in targets:
            try:
                self._execute(target)
            except Exception as exc:  # noqa: BLE001 - 单条失败不阻断其余目标
                failures.append(
                    CleanupFailure(
                        kind=target.kind,
                        target_id=target.target_id,
                        reason=f"{type(exc).__name__}: {exc}",
                        transient=isinstance(exc, OSError),
                    )
                )
                continue
            cleaned_counts[target.kind] = cleaned_counts.get(target.kind, 0) + 1
        return CleanupReport(
            found=len(targets),
            cleaned_counts=cleaned_counts,
            failures=tuple(failures),
        )

    def _discover(self) -> list[CleanupTarget]:
        """发现全部可清理目标（索引残留 -> 孤儿派生资产 -> 产物目录）"""
        targets: list[CleanupTarget] = []
        now = datetime.fromisoformat(self._now_fn())
        cutoff = now - RESIDUE_MAX_AGE

        indexes = self._index_repo.list_all()
        referenced_collections: set[str] = set()
        referenced_namespaces: set[str] = set()
        for index in indexes:
            referenced_collections.add(
                index.vector_collection or collection_name(index.id)
            )
            referenced_namespaces.add(
                index.fts_namespace or fts_namespace_name(index.id)
            )
            if index.status is IndexVersionStatus.ACTIVE:
                continue
            if index.status is IndexVersionStatus.RETIRED:
                if self._retired_assets_remain(index):
                    targets.append(CleanupTarget(TARGET_RETIRED_INDEX, index.id))
                continue
            # 未激活过的残留行（staging/validating/failed）：到期且无在途任务
            created = datetime.fromisoformat(index.created_at)
            has_inflight = (
                self._task_repo.count_non_terminal_by_document_version(
                    index.document_version_id
                )
                > 0
            )
            if created <= cutoff and not has_inflight:
                targets.append(CleanupTarget(TARGET_RESIDUAL_INDEX, index.id))

        for name in self._vector_index.list_collections():
            if name.startswith(COLLECTION_PREFIX) and name not in referenced_collections:
                targets.append(CleanupTarget(TARGET_ORPHAN_COLLECTION, name))
        for name in self._keyword_index.list_namespaces():
            if name.startswith(FTS_NAMESPACE_PREFIX) and name not in referenced_namespaces:
                targets.append(CleanupTarget(TARGET_ORPHAN_NAMESPACE, name))

        targets.extend(self._discover_result_dirs())
        return targets

    def _discover_result_dirs(self) -> list[CleanupTarget]:
        """发现可清理的云端产物目录：任务终态或无对应任务行即残留"""
        if not os.path.isdir(self._work_dir):
            return []
        targets = []
        for entry in os.listdir(self._work_dir):
            task = self._task_repo.get(entry)
            if task is None or task.state in TERMINAL_STATES:
                targets.append(CleanupTarget(TARGET_RESULT_DIR, entry))
        return targets

    def _execute(self, target: CleanupTarget) -> None:
        """执行单条清理（幂等，重复执行无额外副作用）"""
        if target.kind in (TARGET_RETIRED_INDEX, TARGET_RESIDUAL_INDEX):
            self._clean_index_assets(target.target_id, drop_row=target.kind == TARGET_RESIDUAL_INDEX)
        elif target.kind == TARGET_ORPHAN_COLLECTION:
            self._vector_index.delete_collection(target.target_id)
        elif target.kind == TARGET_ORPHAN_NAMESPACE:
            # 命名空间即行集合：清空即删除，无独立对象残留
            self._keyword_index.rebuild_namespace(target.target_id, [])
        elif target.kind == TARGET_RESULT_DIR:
            self._remove_result_dir(target.target_id)
        else:
            raise ValueError(f"未知清理目标类别: {target.kind}")

    def _clean_index_assets(self, index_id: str, *, drop_row: bool) -> None:
        """清理索引版本的派生资产（先外部资产，后切片行，最后索引行）"""
        index = self._index_repo.get(index_id)
        if index is None:
            return
        collection = index.vector_collection or collection_name(index.id)
        namespace = index.fts_namespace or fts_namespace_name(index.id)
        self._vector_index.delete_collection(collection)
        self._keyword_index.rebuild_namespace(namespace, [])
        self._chunk_repo.delete_index_chunks(index.id)
        if drop_row:
            self._index_repo.delete(index.id)

    def _retired_assets_remain(self, index: IndexVersion) -> bool:
        """退役索引是否仍有派生资产（已清空的行不再成为清扫目标）"""
        collection = index.vector_collection or collection_name(index.id)
        namespace = index.fts_namespace or fts_namespace_name(index.id)
        return (
            self._vector_index.count_vectors(collection) > 0
            or self._keyword_index.count_documents(namespace) > 0
            or self._chunk_repo.count_index_chunks(index.id) > 0
        )

    def _remove_result_dir(self, entry: str) -> None:
        """删除云端产物目录条目（目录或文件），路径必须仍位于工作目录内"""
        root = os.path.realpath(self._work_dir)
        path = os.path.realpath(os.path.join(self._work_dir, entry))
        if os.path.commonpath([root, path]) != root:
            raise PathUnsafeError("产物目录路径逃逸受控工作目录")
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
