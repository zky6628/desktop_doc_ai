# -*- coding: utf-8 -*-
"""评测运行编排：切片参数组的自动化对比（设参 -> 重建 -> 跑题 -> 聚合）

一次运行对同一问题集依次施加多组切片参数：每组先写在役参数并对
参评文档版本执行重建（重建管线由 Worker 注入回调，以当前在役参数
执行完整管线并原子激活），再串行执行问题集（经查询编排器走真实
链路，指标事实照常落库），组内聚合为对比指标快照。全部组完成后
恢复评测前参数并重建回原配置——索引资产无遗留实验状态。同参数
重建的嵌入经内容哈希缓存命中，恢复步骤零 API 成本。

独占语义：同一时刻至多一个运行中评测（在役参数是全局状态，并发
评测必然互踩）。取消在组间/问题间检查点生效，取消与失败路径同样
执行参数与索引恢复（恢复失败以日志表达，不掩盖原始终态）。
"""
import json
import logging
import time
from collections.abc import Callable, Sequence

from app.domain.chunking import (
    CHUNKING_OVERRIDE_SETTING_KEY,
    ChunkingParams,
    chunk_blocks,
)
from app.domain.entities import EvaluationRun, EvaluationRunState
from app.domain.ports import (
    ContentRepository,
    EvaluationRunRepository,
    SystemSettingsRepository,
)

logger = logging.getLogger(__name__)

# 规模预检上限：预估切片总量超过该值拒绝创建（嵌入费用失控防线）
MAX_EVALUATION_CHUNKS = 50_000

# 查询终态轮询间隔（秒）：查询由编排器后台线程执行，此处等待终态
_QUERY_POLL_SECONDS = 0.1

# 查询等待上限（秒）：单问题生成卡死的兜底断言
_QUERY_TIMEOUT_SECONDS = 600

# 组结果中指标行的统一读取口径（指标行 state 的字符串值）
_METRIC_STATE_COMPLETED = "completed"
_METRIC_STATE_FAILED = "failed"
_METRIC_STATE_CANCELLED = "cancelled"

# 问题等待的终态取值（与指标行 state 同源）
_TERMINAL_STATE_VALUES = frozenset(
    {_METRIC_STATE_COMPLETED, _METRIC_STATE_FAILED, _METRIC_STATE_CANCELLED}
)


class EvaluationCancelled(Exception):
    """评测取消信号：检查点检测到取消请求时抛出"""


class EvaluationRunService:
    """评测运行编排器：预检、执行与聚合"""

    def __init__(
        self,
        *,
        evaluation_repo: EvaluationRunRepository,
        settings_repo: SystemSettingsRepository,
        content_repo: ContentRepository,
        version_ids_reader: Callable[[str], list[str]],
        query_launcher: Callable[[str, str], str],
        query_metrics_reader: Callable[[str], dict],
    ) -> None:
        """评测编排器

        :param version_ids_reader: 知识库当前活动文档版本列表读取器
        :param query_launcher: 启动单问题查询，返回查询运行 ID
            （Worker 装配时桥接查询编排器；后台线程执行）
        :param query_metrics_reader: 读取查询运行的聚合口径行
            （state/refused/rerank_degraded/server_ttft_ms/tokens）
        """
        self._evaluation_repo = evaluation_repo
        self._settings_repo = settings_repo
        self._content_repo = content_repo
        self._version_ids_reader = version_ids_reader
        self._query_launcher = query_launcher
        self._query_metrics_reader = query_metrics_reader

    def version_ids_for_kb(self, knowledge_base_id: str) -> list[str]:
        """知识库参评版本快照：当前全部活动文档版本"""
        return self._version_ids_reader(knowledge_base_id)

    # ---------- 预检 ----------

    def estimate_chunk_count(
        self, version_ids: Sequence[str], params: ChunkingParams
    ) -> int:
        """按给定参数对参评版本模拟切片，返回预估切片总量

        切片是纯函数：以已落库解析块内存模拟，估算精确（非抽样近
        似）；耗时与块数线性，创建时一次性支出可接受
        """
        total = 0
        for version_id in version_ids:
            blocks = self._content_repo.list_document_blocks(version_id)
            total += len(chunk_blocks(blocks, params))
        return total

    # ---------- 执行 ----------

    def execute(
        self,
        run: EvaluationRun,
        *,
        rebuild_version: Callable[[str], None],
        is_cancel_requested: Callable[[], bool],
    ) -> None:
        """执行评测运行（阻塞直至终态）

        评测前参数在 finally 中无条件回写（瞬时操作）；索引恢复重建
        由调用方在任务收尾后入队（任务终态后管线无法再推进其阶段，
        队列化恢复让用户在任务中心可见恢复进度，同参数重建经嵌入
        缓存近零成本）。

        :param rebuild_version: 单文档版本重建回调（Worker 注入）
        :param is_cancel_requested: 取消检测回调（Worker 注入）
        """
        try:
            self._execute_groups(run, rebuild_version, is_cancel_requested)
        except EvaluationCancelled:
            self._evaluation_repo.mark_terminal(
                run.id, EvaluationRunState.CANCELLED
            )
            return
        except Exception:
            logger.exception("评测运行 %s 执行异常", run.id)
            self._evaluation_repo.mark_terminal(
                run.id, EvaluationRunState.FAILED, error_code="INTERNAL_ERROR"
            )
            return
        self._evaluation_repo.mark_terminal(run.id, EvaluationRunState.COMPLETED)

    def _execute_groups(
        self,
        run: EvaluationRun,
        rebuild_version: Callable[[str], None],
        is_cancel_requested: Callable[[], bool],
    ) -> None:
        """逐组执行：设参 -> 重建 -> 跑题 -> 聚合；finally 恢复参数"""
        original = self._settings_repo.get(CHUNKING_OVERRIDE_SETTING_KEY)
        try:
            for group_index, group in enumerate(run.param_groups):
                self._check_cancelled(is_cancel_requested, group_index, 0)
                self._write_params(group)
                self._rebuild_all(run, rebuild_version)
                query_run_ids: list[str] = []
                for question_index, question in enumerate(run.questions):
                    self._check_cancelled(is_cancel_requested, group_index, question_index)
                    query_run_ids.append(
                        self._run_question(run.knowledge_base_id, question)
                    )
                    self._evaluation_repo.mark_progress(
                        run.id, group_index, question_index + 1
                    )
                result = self._aggregate_group(group, query_run_ids)
                self._evaluation_repo.save_group_result(
                    run.id, group_index, result
                )
        finally:
            self._restore_params(original)

    def _run_question(self, kb_id: str, question: str) -> str:
        """执行单个问题并等待终态，返回查询运行 ID"""
        run_id = self._query_launcher(kb_id, question)
        deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
        while True:
            metrics = self._query_metrics_reader(run_id)
            if metrics["state"] in _TERMINAL_STATE_VALUES:
                return run_id
            if time.monotonic() > deadline:
                raise TimeoutError(f"评测问题执行超时: {question[:50]}")
            time.sleep(_QUERY_POLL_SECONDS)

    def _aggregate_group(
        self, group: dict, query_run_ids: list[str]
    ) -> dict:
        """聚合单组指标：状态分布、TTFT 分位与 token 合计"""
        rows = [self._query_metrics_reader(rid) for rid in query_run_ids]
        ttfts = sorted(
            row["server_ttft_ms"]
            for row in rows
            if row["server_ttft_ms"] is not None
        )

        def _percentile(sorted_values: list[int], ratio: float) -> int | None:
            if not sorted_values:
                return None
            index = min(
                round(ratio * (len(sorted_values) - 1)),
                len(sorted_values) - 1,
            )
            return sorted_values[index]

        return {
            "group": group,
            "query_run_ids": list(query_run_ids),
            "metrics": {
                "total": len(rows),
                "completed": sum(
                    1 for row in rows if row["state"] == _METRIC_STATE_COMPLETED
                ),
                "failed": sum(
                    1 for row in rows if row["state"] == _METRIC_STATE_FAILED
                ),
                "cancelled": sum(
                    1 for row in rows if row["state"] == _METRIC_STATE_CANCELLED
                ),
                "refused": sum(1 for row in rows if row["refused"]),
                "rerank_degraded": sum(1 for row in rows if row["rerank_degraded"]),
                "ttft_p50_ms": _percentile(ttfts, 0.50),
                "ttft_p95_ms": _percentile(ttfts, 0.95),
                "input_tokens": sum(row["input_tokens"] or 0 for row in rows),
                "output_tokens": sum(row["output_tokens"] or 0 for row in rows),
            },
        }

    # ---------- 参数与重建 ----------

    def _write_params(self, group: dict) -> None:
        self._settings_repo.put(
            CHUNKING_OVERRIDE_SETTING_KEY,
            json.dumps(
                {
                    "parent_chunk_chars": group["parent_chunk_chars"],
                    "child_chunk_chars": group["child_chunk_chars"],
                }
            ),
        )

    def _restore_params(self, original: str | None) -> None:
        if original is None:
            self._settings_repo.delete(CHUNKING_OVERRIDE_SETTING_KEY)
        else:
            self._settings_repo.put(CHUNKING_OVERRIDE_SETTING_KEY, original)

    def _rebuild_all(
        self,
        run: EvaluationRun,
        rebuild_version: Callable[[str], None],
    ) -> None:
        for version_id in run.target_version_ids:
            rebuild_version(version_id)

    def _check_cancelled(
        self,
        is_cancel_requested: Callable[[], bool],
        group_index: int,
        question_index: int,
    ) -> None:
        if is_cancel_requested():
            raise EvaluationCancelled(
                f"group={group_index} question={question_index}"
            )
