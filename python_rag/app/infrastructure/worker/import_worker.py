# -*- coding: utf-8 -*-
"""导入任务 Worker：领取任务、执行导入/索引重建/补偿清理并落库结果

编排模型：单线程循环按任务类型分派——导入任务按阶段判定执行路径
（本地解析、云端全流程或断点续跑）；索引重建任务从已落库解析事实
重放管线到原子激活；清理任务执行全量幂等清扫。阶段边界统一执行
租约复核与取消检查：租约复核借道心跳续约，失效即终止本次处理，
杜绝无租约写入；取消请求在检查点收尾为取消终态。

批次事实以外部任务表为唯一事实源：已登记批次直接恢复轮询，不重复
提交批次。轮询等待可被停止事件中断，停止时任务留在等待外部结果
状态，租约自然过期后由恢复流程重排续跑。

失败分流：解析输入类错误与认证/配额/协议类云端错误转入失败终态；
瞬态传输与轮询超时走自动重试（退避节奏由任务引擎统一定义）；供应
方对单文件的失败结果不自动重试（重轮询不会改变终态结果，人工重试
将以新批次重新提交），原因文本随失败信息落库供用户诊断。
"""
import json
import logging
import os
import shutil
import threading
import time

from app.domain import embedding, keyword, parser_routing
from app.domain.chunking import (
    CHUNKING_CONFIG_TYPE,
    StoredChunk,
    chunk_blocks,
    chunking_config_json,
    integrity_hash,
)
from app.domain.entities import (
    DocumentVersion,
    ExternalTask,
    IndexVersion,
    IndexVersionStatus,
    Task,
    TaskStage,
    TaskStatus,
)
from app.domain.errors import (
    CloudAuthError,
    CloudParsingError,
    CloudProtocolViolationError,
    CloudTimeoutError,
    CloudTransportError,
    EmbeddingAuthError,
    EmbeddingError,
    EmbeddingTransientError,
    EntityNotFoundError,
    IndexValidationError,
    ParsingError,
    RepositoryError,
    TaskLeaseLostError,
    TaskQueueFullError,
    TaskStateConflictError,
    UnsupportedFormatError,
)
from app.domain.index_maintenance import collection_name, fts_namespace_name
from app.domain.keyword import KeywordDocument
from app.domain.parsing import ParsedDocument
from app.domain.ports import (
    ChunkRepository,
    ContentRepository,
    DocumentRepository,
    DocumentVersionRepository,
    EmbeddingGateway,
    ExternalTaskRepository,
    IndexVersionRepository,
    KeywordIndexGateway,
    PipelineConfigRepository,
    TaskRepository,
    TextTokenizer,
    VectorIndexGateway,
)
from app.domain.task_state import HEARTBEAT_INTERVAL_SECONDS
from app.infrastructure.maintenance import IndexCleanupService
from app.infrastructure.mineru import (
    MinerUClient,
    normalize_archive,
    poll_batch_until_done,
)
from app.infrastructure.mineru.dto import (
    BatchFileEntry,
    BatchPollResult,
    ProviderFileStatus,
)
from app.infrastructure.parsing import (
    DocxParser,
    LocalFileParser,
    PdfTextParser,
    TxtMarkdownParser,
)

logger = logging.getLogger(__name__)

# 队列空转时的等待间隔（秒）：无任务可领取时的轮询节奏
_IDLE_SECONDS = 1.0

# 周期兜底扫描间隔（秒）：无导入活动时残留清理的发现节奏。
# 成功激活后的清理任务即时覆盖主路径，本扫描保证长期驻留进程的
# 失败/取消残留也不会超过保留期限
_SWEEP_SCAN_INTERVAL_SECONDS = 6 * 3600

# 清理补偿任务的派生深度上限：补偿任务失败时自动再派生一层，
# 达到上限后停止（防永久不可清理资源导致任务无限派生）
_MAX_CLEANUP_COMPENSATION_DEPTH = 3

# 各阶段的标准进度取值：阶段推进时写入，供任务中心展示
# （嵌入阶段的进度随批次推进单独计算，不使用静态值）
_STAGE_PROGRESS: dict[TaskStage, float] = {
    TaskStage.PARSING_LOCAL: 0.2,
    TaskStage.SUBMITTING_CLOUD: 0.35,
    TaskStage.POLLING_CLOUD: 0.5,
    TaskStage.DOWNLOADING_CLOUD_RESULT: 0.6,
    TaskStage.NORMALIZING: 0.7,
    TaskStage.CHUNKING: 0.8,
    TaskStage.WRITING_VECTOR_INDEX: 0.94,
    TaskStage.WRITING_KEYWORD_INDEX: 0.96,
    TaskStage.VALIDATING_INDEX: 0.97,
    TaskStage.ACTIVATING_VERSION: 0.99,
    TaskStage.CLEANING_UP: 0.5,
    TaskStage.COMPLETED: 1.0,
}

# 嵌入批次进度区间：在切片完成与向量写入完成之间线性推进
_EMBED_PROGRESS_BASE = 0.85
_EMBED_PROGRESS_SPAN = 0.05

# 本地解析器注册表：键为受控暂存扩展名（上传白名单保证取值受控）。
# 解析器无状态，可安全复用实例
_LOCAL_PARSERS: dict[str, LocalFileParser] = {
    "txt": TxtMarkdownParser(markdown_mode=False),
    "md": TxtMarkdownParser(markdown_mode=True),
    "markdown": TxtMarkdownParser(markdown_mode=True),
    "docx": DocxParser(),
    "pdf": PdfTextParser(),
}


class _TaskInterrupted(Exception):
    """处理流程内部控制流异常：任务已被取消或租约丢失

    不向主循环传播：取消已在检查点收尾，租约丢失交由恢复流程接管
    """


class _ProviderParsingFailed(CloudParsingError):
    """供应方对批次内文件的解析结果为失败

    失败原因只有供应方文本描述，无法机读分类；重试同一批次不会
    改变结果，故按不可自动重试处理，人工重试将以新批次重新提交
    """


def _read_route(input_json: str | None) -> dict:
    """解析任务输入中的路由决策记录

    :param input_json: 导入事务写入的路由决策 JSON 文本
    :return: 路由决策字典（mode/parser_preference 等）
    :raises ValueError: 输入缺失或结构不符（内部事实被破坏，按内部
        错误转入失败终态，不盲目猜测路径）
    """
    if not input_json:
        raise ValueError("任务输入缺少路由决策")
    route = json.loads(input_json)
    if not isinstance(route, dict) or "mode" not in route:
        raise ValueError("任务输入的路由决策结构不符")
    return route


def _read_compensation_depth(input_json: str | None) -> int:
    """读取清理任务的补偿深度（未提供为 0）

    :param input_json: 清理任务输入 JSON 文本
    :return: 已派生的补偿层数
    :raises (ValueError, TypeError): 输入结构不符（内部事实被破坏，
        按内部错误转入失败终态，不盲目猜测深度）
    """
    if not input_json:
        return 0
    payload = json.loads(input_json)
    if not isinstance(payload, dict):
        raise TypeError("清理任务输入的结构不符")
    depth = payload.get("compensation_depth", 0)
    if not isinstance(depth, int) or isinstance(depth, bool):
        raise TypeError("清理任务的补偿深度取值类型不符")
    return depth


def _remove_tree(path: str) -> None:
    """尽力删除云端产物工作目录：成功必清，失败留给周期补偿清理"""
    shutil.rmtree(path, ignore_errors=True)


class ImportTaskWorker:
    """导入任务 Worker（单线程循环，解析事实落库的唯一写入方）

    :param task_repo: 任务仓储
    :param document_repo: 文档仓储
    :param version_repo: 文档版本仓储
    :param content_repo: 内容仓储（解析事实落库与读取）
    :param external_repo: 外部任务仓储（批次事实唯一事实源）
    :param index_repo: 索引版本仓储（staging 索引复用、验证结果与激活）
    :param chunk_repo: 切片仓储（切片与定位关系写入与读取）
    :param config_repo: 流水线配置仓储（切片/嵌入/关键词配置版本行）
    :param embedding_gateway: 文本向量化网关；未配置时嵌入阶段按
        认证失败处理（解析与切片路线不受影响）
    :param vector_index: 向量索引网关（staging 集合写入与验证）
    :param text_tokenizer: 文本分词器（关键词索引预分词）
    :param keyword_index: 关键词索引网关（FTS 命名空间写入与验证）
    :param mineru_client: 云端解析客户端；未配置时云端任务在提交时
        按认证失败处理（本地路线不受影响）
    :param work_dir: 云端产物受控工作目录（结果下载与解压）
    :param worker_id: 租约持有者标识
    """

    def __init__(
        self,
        *,
        task_repo: TaskRepository,
        document_repo: DocumentRepository,
        version_repo: DocumentVersionRepository,
        content_repo: ContentRepository,
        external_repo: ExternalTaskRepository,
        index_repo: IndexVersionRepository,
        chunk_repo: ChunkRepository,
        config_repo: PipelineConfigRepository,
        embedding_gateway: EmbeddingGateway | None,
        vector_index: VectorIndexGateway,
        text_tokenizer: TextTokenizer,
        keyword_index: KeywordIndexGateway,
        mineru_client: MinerUClient | None,
        work_dir: str,
        worker_id: str,
    ) -> None:
        self._task_repo = task_repo
        self._document_repo = document_repo
        self._version_repo = version_repo
        self._content_repo = content_repo
        self._external_repo = external_repo
        self._index_repo = index_repo
        self._chunk_repo = chunk_repo
        self._config_repo = config_repo
        self._embedding_gateway = embedding_gateway
        self._vector_index = vector_index
        self._text_tokenizer = text_tokenizer
        self._keyword_index = keyword_index
        self._mineru = mineru_client
        self._work_dir = work_dir
        self._worker_id = worker_id
        self._cleanup = IndexCleanupService(
            index_repo=index_repo,
            chunk_repo=chunk_repo,
            task_repo=task_repo,
            vector_index=vector_index,
            keyword_index=keyword_index,
            work_dir=work_dir,
        )
        self._stop = threading.Event()

    def stop(self) -> None:
        """请求停止：主循环与心跳在当前等待间隔内退出

        进行中的任务不做收尾（任务留待租约过期后由恢复流程重排），
        保证停止等待是常数级而非任务时长级
        """
        self._stop.set()

    def start_background(self) -> threading.Thread:
        """以 daemon 线程启动主循环，返回线程引用（应用装配用）"""
        thread = threading.Thread(
            target=self.run_forever,
            name=f"import-worker-{self._worker_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def run_forever(self) -> None:
        """阻塞执行主循环：恢复中断任务 -> 周期兜底扫描 -> 领取并处理

        单次循环失败只记录并继续，不终止 Worker；停止事件触发后
        在当前等待点退出
        """
        next_scan = time.monotonic()
        while not self._stop.is_set():
            try:
                self._task_repo.recover_interrupted_tasks()
                if time.monotonic() >= next_scan:
                    self._spawn_cleanup_task_if_residue()
                    next_scan = time.monotonic() + _SWEEP_SCAN_INTERVAL_SECONDS
                processed = self.process_next()
            except Exception as exc:  # noqa: BLE001 - 循环边界兜底：单次失败不终止 Worker
                logger.warning(
                    "Worker 循环异常继续: %s: %s", type(exc).__name__, exc
                )
                processed = False
            if not processed:
                self._stop.wait(_IDLE_SECONDS)

    def _spawn_cleanup_task_if_residue(self) -> None:
        """周期兜底扫描：发现可清理残留时登记清理任务

        只读扫描发现残留才建任务，干净系统不产生任务噪音；队列满时
        跳过，由下一轮扫描接续
        """
        if not self._cleanup.find_targets():
            return
        try:
            self._task_repo.create("cleanup")
            logger.info("发现可清理残留，已登记清理任务")
        except TaskQueueFullError:
            logger.warning("清理任务创建被拒（队列已满），留待下轮扫描")

    def process_next(self) -> bool:
        """领取并处理一个任务（导入/索引重建/补偿清理）

        :return: 本轮是否处理了任务（无可领取任务为 False）
        """
        task = self._task_repo.claim_next(self._worker_id)
        if task is None:
            return False
        logger.info(
            "领取任务 %s (type=%s, stage=%s)",
            task.id,
            task.task_type,
            task.stage.value if task.stage else "无",
        )
        heartbeat_stop = self._start_heartbeat(task.id)
        try:
            self._process(task)
        except _TaskInterrupted:
            pass
        except Exception as exc:  # noqa: BLE001 - 任务边界兜底：未预期异常转失败终态而非遗留执行中
            logger.warning(
                "任务处理异常 %s: %s: %s", task.id, type(exc).__name__, exc
            )
            self._fail(
                task.id, "INTERNAL_ERROR", f"处理异常终止: {type(exc).__name__}"
            )
        finally:
            heartbeat_stop.set()
        return True

    def _start_heartbeat(self, task_id: str) -> threading.Event:
        """为当前处理中的任务启动心跳续约线程，返回停止标志

        心跳失败不中断处理：阶段边界的租约复核承担丢失防护
        """
        stop = threading.Event()

        def _beat() -> None:
            while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
                try:
                    self._task_repo.heartbeat(task_id, self._worker_id)
                except RepositoryError as exc:
                    # 心跳失败不中断处理：阶段边界的租约复核承担丢失防护
                    logger.warning("心跳失败 %s: %s", task_id, exc)

        threading.Thread(
            target=_beat, name=f"heartbeat-{task_id}", daemon=True
        ).start()
        return stop

    def _process(self, task: Task) -> None:
        """按阶段判定执行路径并处理；领域错误在此分类落定"""
        try:
            self._dispatch(task)
        except _TaskInterrupted:
            raise
        except ParsingError as exc:
            # 解析输入类：空内容/损坏/加密/编码，不可自动重试
            self._fail(task.id, exc.error_code, str(exc))
        except UnsupportedFormatError as exc:
            self._fail(task.id, "UNSUPPORTED_FORMAT", str(exc))
        except (CloudTimeoutError, CloudTransportError, EmbeddingTransientError) as exc:
            # 瞬态传输与轮询/嵌入失败：按任务引擎的退避节奏自动重试
            self._retry(task.id, exc.error_code, str(exc))
        except CloudParsingError as exc:
            self._fail(task.id, exc.error_code, str(exc))
        except EmbeddingError as exc:
            # 认证/配额/协议类嵌入失败：不可自动重试
            self._fail(task.id, exc.error_code, str(exc))
        except IndexValidationError as exc:
            # 确定性索引校验失败：不可自动重试，staging 留待补偿清理
            self._fail(task.id, exc.error_code, str(exc))
        except TaskLeaseLostError:
            logger.warning("租约丢失，终止任务处理 %s", task.id)
            raise _TaskInterrupted() from None

    def _dispatch(self, task: Task) -> None:
        """按任务类型与阶段判定执行路径

        导入任务的阶段是路径判定的权威依据：确认服务批准后把阶段
        推进到云端提交，使升级路线无需额外的标记字段；云端各中断点
        的续跑由外部任务记录与阶段共同决定
        """
        self._checkpoint(task.id)
        if task.task_type == "cleanup":
            self._run_cleanup(task)
            return
        if task.task_type == "rebuild_index":
            self._run_rebuild(task)
            return
        if task.task_type != "import":
            raise RuntimeError(f"不支持的任务类型: {task.task_type}")
        stage = task.stage
        if stage is None:
            route = _read_route(task.input_json)
            if route.get("mode") == parser_routing.ParserRouteMode.CLOUD.value:
                self._run_cloud(task)
            else:
                self._run_local(task, route)
        elif stage is TaskStage.PARSING_LOCAL:
            self._run_local(task, _read_route(task.input_json))
        elif stage in (TaskStage.SUBMITTING_CLOUD, TaskStage.POLLING_CLOUD):
            # 提交/轮询中断后续跑：批次记录存在与否决定提交或恢复轮询
            self._run_cloud(task)
        elif stage in (TaskStage.DOWNLOADING_CLOUD_RESULT, TaskStage.NORMALIZING):
            self._finish_cloud(task)
        elif stage is TaskStage.CHUNKING:
            # 切片中断续跑：解析事实已落库，直接重入切片（幂等）
            self._run_chunking(task, self._require_version(task))
        elif stage in (
            TaskStage.EMBEDDING,
            TaskStage.WRITING_VECTOR_INDEX,
            TaskStage.WRITING_KEYWORD_INDEX,
            TaskStage.VALIDATING_INDEX,
            TaskStage.ACTIVATING_VERSION,
        ):
            # 索引管线各中断点续跑：批次游标与幂等写入保证只补尾部
            self._run_vector_pipeline(task, self._require_version(task))
        else:
            raise RuntimeError(f"导入任务阶段不在可执行范围: {stage.value}")

    def _run_local(self, task: Task, route: dict) -> None:
        """本地路线：解析 -> 落库 -> 成功；扫描件信号转入用户确认"""
        self._set_stage(task.id, TaskStage.PARSING_LOCAL)
        version = self._require_version(task)

        extension = os.path.splitext(version.source_path)[1].lstrip(".").lower()
        parser = _LOCAL_PARSERS.get(extension)
        if parser is None:
            raise UnsupportedFormatError(f"没有可用的本地解析器: .{extension}")
        parsed = parser.parse(version.source_path, version.id)

        if parsed.scan_suspected:
            if parser_routing.requires_cloud_confirmation(
                scan_suspected=True,
                preference=route.get("parser_preference", "auto"),
            ):
                # 本地解析器无法从扫描件提取文本：转等待用户确认，
                # 确认后经云端路线重新处理
                self._task_repo.transition(
                    task.id,
                    TaskStatus.WAITING_USER,
                    stage=TaskStage.ROUTING_PARSER,
                )
                logger.info("任务转入用户确认 %s", task.id)
                return
            # 用户已明确指定云端解析：本地无产出，直接进入云端提交
            self._run_cloud(task)
            return

        self._persist_parsed(task, version, parsed)

    def _run_cleanup(self, task: Task) -> None:
        """补偿清理：执行一次全量幂等清扫并记录审计摘要

        单条目标失败不阻断清扫；全部失败均为文件系统瞬态错误时走
        自动重试，否则转失败终态并由有界补偿任务接续
        """
        self._set_stage(task.id, TaskStage.CLEANING_UP)
        try:
            report = self._cleanup.sweep()
        except OSError as exc:
            # 目标发现阶段的整体失败（目录/向量库不可达）：按瞬态重试
            self._retry(task.id, "CLEANUP_TRANSIENT", str(exc))
            return
        self._task_repo.append_event(
            task.id, "cleanup_summary", detail_json=report.summary_json()
        )
        if not report.failures:
            self._succeed(task.id)
            return
        if all(failure.transient for failure in report.failures):
            finished = self._retry(
                task.id,
                "CLEANUP_TRANSIENT",
                f"{len(report.failures)} 项清理目标暂不可达",
            )
        else:
            finished = self._fail(
                task.id,
                "CLEANUP_INCOMPLETE",
                f"{len(report.failures)} 项清理目标无法删除",
            )
        if finished is not None and finished.state is TaskStatus.FAILED:
            self._spawn_cleanup_compensation(task)

    def _spawn_cleanup_compensation(self, failed: Task) -> None:
        """清理终态失败后创建有界补偿任务

        每层补偿携带派生深度，达到上限后停止自动派生（防永久不可
        清理的资源导致任务无限增长），残留由周期扫描与人工重试兜底
        """
        depth = _read_compensation_depth(failed.input_json)
        if depth >= _MAX_CLEANUP_COMPENSATION_DEPTH:
            logger.warning(
                "清理补偿深度已达上限，停止自动派生 %s", failed.id
            )
            return
        try:
            self._task_repo.create(
                "cleanup",
                parent_task_id=failed.id,
                input_json=json.dumps(
                    {"compensation_depth": depth + 1},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            logger.info("已创建清理补偿任务（深度 %d）", depth + 1)
        except TaskQueueFullError:
            logger.warning("清理补偿任务创建被拒（队列已满），留待周期扫描")

    def _run_rebuild(self, task: Task) -> None:
        """索引重建：从已落库解析事实重建双索引并原子切换

        目标文档版本须已完成解析（供健康检查确认派生索引损坏后
        发起）；切片从解析事实确定性重放，向量以真实网关重嵌，新
        索引验证通过前不触碰既有活动索引，任何失败零损伤
        """
        self._run_chunking(task, self._require_version(task))

    def _run_cloud(self, task: Task) -> None:
        """云端路线：提交（或恢复既有批次）-> 轮询 -> 转下载阶段"""
        external = self._find_external(task.id)
        if external is None:
            external = self._submit_cloud(task)
        status = self._poll_until_done(task, external)
        if status.state != "done":
            raise _ProviderParsingFailed(
                f"云端解析失败: {status.err_msg or status.status_summary}"
            )
        # 轮询完成即释放执行槽：下载/归一化经重新领取续跑
        self._task_repo.transition(
            task.id, TaskStatus.QUEUED, stage=TaskStage.DOWNLOADING_CLOUD_RESULT
        )

    def _submit_cloud(self, task: Task) -> ExternalTask:
        """提交批次并上传源文件，登记批次关联后转入等待外部结果

        提交顺序遵循摄取合同：成功上传后先持久化批次/源关联，再释放
        执行槽。登记前中断会以新批次重新提交（秒级窗口，远端批次自行
        过期）；登记后任何中断均以既有批次恢复轮询，不重复提交
        """
        client = self._require_client()
        self._set_stage(task.id, TaskStage.SUBMITTING_CLOUD)
        version = self._require_version(task)
        document_id = task.document_id
        if document_id is None:
            raise RuntimeError(f"任务缺少文档引用: {task.id}")
        document = self._document_repo.get(document_id)
        if document is None:
            raise RuntimeError(f"任务引用的文档不存在: {document_id}")

        submission = client.create_batch(
            [BatchFileEntry(source_ref=version.id, display_name=document.display_name)]
        )
        client.upload_file(submission.upload_urls[0], version.source_path)
        external = self._external_repo.register(
            task_id=task.id,
            provider=MinerUClient.PROVIDER_NAME,
            provider_batch_ref=submission.batch_id,
            source_ref=version.id,
            upload_url_expires_at=submission.upload_url_expires_at,
            request_summary_json=json.dumps(
                {"file_count": 1}, ensure_ascii=False, separators=(",", ":")
            ),
        )
        # 关联已持久化：转入等待外部结果（该状态不占执行槽）
        self._task_repo.transition(
            task.id, TaskStatus.WAITING_EXTERNAL, stage=TaskStage.POLLING_CLOUD
        )
        return external

    def _poll_until_done(
        self, task: Task, external: ExternalTask
    ) -> ProviderFileStatus:
        """轮询批次直到本任务源文件进入终态；轮询事实逐次落库"""
        client = self._require_client()

        def _sleep(seconds: float) -> None:
            # 等待可被停止事件唤醒，保证进程关闭不被长轮询拖住
            self._stop.wait(seconds)

        statuses = poll_batch_until_done(
            client,
            external.provider_batch_ref,
            sleep=_sleep,
            on_poll=lambda result, _elapsed: self._on_poll(task.id, external, result),
        )
        status = statuses[external.source_ref]
        logger.info("云端解析终态 %s: %s", task.id, status.status_summary)
        return status

    def _on_poll(
        self, task_id: str, external: ExternalTask, result: BatchPollResult
    ) -> None:
        """单次轮询回调：落库轮询事实，并检查停止与取消请求"""
        if self._stop.is_set():
            raise _TaskInterrupted()
        status = next(
            (
                item
                for item in result.files
                if item.source_ref == external.source_ref
            ),
            None,
        )
        if status is not None:
            self._external_repo.record_poll(
                external.id,
                state=status.state,
                status_summary=status.status_summary,
            )
        current = self._task_repo.get(task_id)
        if current is not None and current.state is TaskStatus.CANCEL_REQUESTED:
            self._task_repo.cancel_at_checkpoint(task_id, self._worker_id)
            raise _TaskInterrupted()

    def _finish_cloud(self, task: Task) -> None:
        """下载结果压缩包、安全解压并归一化落库（断点续跑入口）"""
        client = self._require_client()
        external = self._find_external(task.id)
        if external is None:
            raise RuntimeError(f"任务缺少已登记的云端批次: {task.id}")
        version = self._require_version(task)

        self._set_stage(task.id, TaskStage.DOWNLOADING_CLOUD_RESULT)
        # 结果地址不落库（安全合同）：续跑时重新查询批次快照获取
        snapshot = client.poll_batch(external.provider_batch_ref)
        status = next(
            (
                item
                for item in snapshot.files
                if item.source_ref == external.source_ref
            ),
            None,
        )
        if status is None or status.state != "done" or not status.full_zip_url:
            raise CloudProtocolViolationError("批次结果缺少可用的下载地址")
        self._external_repo.record_poll(
            external.id,
            state=status.state,
            status_summary=status.status_summary,
        )

        work_root = os.path.join(self._work_dir, task.id)
        os.makedirs(work_root, exist_ok=True)
        zip_path = os.path.join(work_root, "result.zip")
        digest = client.download_result(status.full_zip_url, zip_path)
        self._external_repo.set_result(external.id, result_sha256=digest)
        extract_dir = os.path.join(work_root, "extracted")
        client.extract_result_archive(zip_path, extract_dir)

        self._set_stage(task.id, TaskStage.NORMALIZING)
        parsed = normalize_archive(extract_dir, version.id)
        self._persist_parsed(task, version, parsed)

    def _persist_parsed(
        self, task: Task, version: DocumentVersion, parsed: ParsedDocument
    ) -> None:
        """解析事实原子提交：内容落库 -> 版本回写 -> 进入切片阶段

        落库前执行最后检查点；提交序列之后若与取消请求竞争，按取消
        语义收尾（已落库的解析事实保留，不伪装为成功）
        """
        self._checkpoint(task.id)
        self._content_repo.replace_document_content(version.id, parsed)
        self._version_repo.mark_parsed(
            version.id,
            parsed_content_sha256=parsed.structure_sha256,
            parser_provider=parsed.parser_provider,
            parser_version=parsed.parser_version,
        )
        try:
            self._run_chunking(task, version)
        except TaskStateConflictError:
            # 阶段推进被拒的唯一可能：取消请求先转入等待取消状态
            self._task_repo.cancel_at_checkpoint(task.id, self._worker_id)

    def _run_chunking(self, task: Task, version: DocumentVersion) -> None:
        """切片阶段：确保配置 -> 复用或创建 staging 索引版本 -> 切片落库

        切片输入读取已落库的解析事实（与内存解析模型解耦），中断
        续跑无需重新解析；写入按序号幂等 upsert，重试不产生重复。
        切片/嵌入/关键词三类配置在索引创建时一并登记，保证索引版本
        可追溯全部构建参数
        """
        self._set_stage(task.id, TaskStage.CHUNKING)
        index_version = self._ensure_index_version(version.id)
        blocks = self._content_repo.list_document_blocks(version.id)
        chunks = chunk_blocks(blocks)
        self._chunk_repo.replace_index_chunks(index_version.id, chunks)
        self._run_vector_pipeline(task, version)

    def _run_vector_pipeline(self, task: Task, version: DocumentVersion) -> None:
        """索引管线：嵌入批次流水 -> 向量写入边界 -> 关键词写入 -> 验证 -> 激活

        嵌入以批次为粒度流式写入 staging 集合并推进游标 checkpoint，
        中断续跑只补尾部批次；关键词索引本地重建（幂等、无外部成本）；
        验证通过后激活事务切换活动指针并回填文档级指针——失败路径
        不触碰任何既有活动索引
        """
        index_version = self._ensure_index_version(version.id)
        stored_chunks = self._chunk_repo.list_index_chunks(index_version.id)
        children = [
            stored for stored in stored_chunks if stored.chunk.parent_ordinal is not None
        ]
        collection = self._collection_name(index_version)

        self._set_stage(task.id, TaskStage.EMBEDDING)
        self._embed_and_write(task, collection, children)
        # 向量写入已随嵌入批次完成：本阶段作为写入完成的边界推进
        self._set_stage(task.id, TaskStage.WRITING_VECTOR_INDEX)
        self._set_stage(task.id, TaskStage.WRITING_KEYWORD_INDEX)
        self._write_keyword_index(index_version.id, children)
        self._set_stage(task.id, TaskStage.VALIDATING_INDEX)
        self._validate_index(index_version.id, collection, children)
        # 激活前最后取消点：激活事务不可中断
        self._checkpoint(task.id)
        self._set_stage(task.id, TaskStage.ACTIVATING_VERSION)
        self._index_repo.activate(index_version.id)
        # 首个活动版本产生即文档就绪；多版本时最新激活胜出
        self._document_repo.set_active_version(version.document_id, version.id)
        # 激活使旧活动索引退役：其派生资产由清理任务回收
        self._spawn_cleanup_task()
        self._succeed(task.id)

    def _spawn_cleanup_task(self) -> None:
        """激活完成后登记清理任务，退役索引的派生资产由此回收"""
        try:
            self._task_repo.create("cleanup")
        except TaskQueueFullError:
            logger.warning("清理任务创建被拒（队列已满），留待周期扫描")

    def _embed_and_write(
        self, task: Task, collection: str, children: list[StoredChunk]
    ) -> None:
        """按批嵌入子切片并写入 staging 集合，逐批推进游标 checkpoint

        先写向量后推进游标：中断重写当前批，向量按记录 ID 幂等，
        续跑只补尾部批次（向量化 API 成本不重复）
        """
        gateway = self._require_gateway()
        total = len(children)
        done_ordinal = self._embedded_cursor(task.checkpoint_json)
        done_count = 0
        for start in range(0, total, embedding.EMBEDDING_BATCH_SIZE):
            batch = children[start : start + embedding.EMBEDDING_BATCH_SIZE]
            if batch[-1].chunk.ordinal <= done_ordinal:
                done_count += len(batch)
                continue
            vectors = gateway.embed_texts(
                [stored.chunk.content for stored in batch]
            )
            self._vector_index.upsert_vectors(
                collection, [stored.id for stored in batch], vectors
            )
            done_count += len(batch)
            self._task_repo.update_stage(
                task.id,
                self._worker_id,
                stage=TaskStage.EMBEDDING,
                progress=self._embedding_progress(done_count, total),
                checkpoint_json=json.dumps(
                    {"embedded_until": batch[-1].chunk.ordinal},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            self._checkpoint(task.id)

    def _validate_index(
        self,
        index_version_id: str,
        collection: str,
        children: list[StoredChunk],
    ) -> None:
        """验证 staging 索引与切片事实一致并记录完整性基准

        向量侧复核数量与 ID 集合；关键词侧复核命名空间文档数；
        完整性哈希覆盖子切片序号与内容哈希序列（Chroma 不存内容
        哈希，该基准供重建与健康检查比对）
        """
        expected_ids = [stored.id for stored in children]
        actual_count = self._vector_index.count_vectors(collection)
        actual_ids = set(self._vector_index.list_vector_ids(collection))
        if actual_count != len(expected_ids) or actual_ids != set(expected_ids):
            raise IndexValidationError(
                f"向量集合与切片事实不一致: 期望 {len(expected_ids)} 条,"
                f" 实际 {actual_count} 条"
            )
        namespace = self._fts_namespace_by_id(index_version_id)
        keyword_count = self._keyword_index.count_documents(namespace)
        if keyword_count != len(children):
            raise IndexValidationError(
                f"关键词索引与切片事实不一致: 期望 {len(children)} 条,"
                f" 实际 {keyword_count} 条"
            )
        self._index_repo.record_validation(
            index_version_id,
            chunk_count=len(children),
            integrity_hash=integrity_hash([stored.chunk for stored in children]),
        )

    def _ensure_index_version(self, document_version_id: str) -> IndexVersion:
        """确保三类配置行就位，复用或创建该文档版本的 staging 索引版本

        同一文档版本同时至多一个在途 staging 索引：切片中断续跑复用
        既有记录（切片/向量/关键词写入均幂等重放），避免每次尝试遗留
        孤儿 staging；任一配置不一致的既有 staging 不复用（留待补偿
        清理），以新索引表达参数变化
        """
        chunking_config_id = self._config_repo.ensure_config(
            CHUNKING_CONFIG_TYPE, chunking_config_json()
        )
        embedding_profile_id = self._config_repo.ensure_config(
            embedding.EMBEDDING_CONFIG_TYPE, embedding.embedding_config_json()
        )
        keyword_config_id = self._config_repo.ensure_config(
            keyword.KEYWORD_CONFIG_TYPE, keyword.keyword_config_json()
        )
        reusable = None
        for index in self._index_repo.list_by_document_version(document_version_id):
            if (
                index.status is IndexVersionStatus.STAGING
                and index.chunking_config_id == chunking_config_id
                and index.embedding_profile_id == embedding_profile_id
            ):
                reusable = index
        if reusable is not None:
            return reusable
        return self._index_repo.create(
            document_version_id,
            chunking_config_id=chunking_config_id,
            embedding_profile_id=embedding_profile_id,
            keyword_config_id=keyword_config_id,
        )

    def _collection_name(self, index_version: IndexVersion) -> str:
        """解析索引版本的向量集合名并保证已登记（幂等）"""
        name = collection_name(index_version.id)
        if index_version.vector_collection != name:
            self._index_repo.set_vector_collection(index_version.id, name)
        return name

    def _write_keyword_index(
        self, index_version_id: str, children: list[StoredChunk]
    ) -> None:
        """关键词索引写入：本地分词后按命名空间整体重建（幂等）"""
        namespace = self._fts_namespace_by_id(index_version_id)
        token_lists = self._text_tokenizer.tokenize(
            [stored.chunk.content for stored in children]
        )
        documents = [
            KeywordDocument(chunk_id=stored.id, content=" ".join(tokens))
            for stored, tokens in zip(children, token_lists)
        ]
        self._keyword_index.rebuild_namespace(namespace, documents)

    def _fts_namespace(self, index_version: IndexVersion) -> str:
        """解析索引版本的 FTS 命名空间并保证已登记（幂等）"""
        namespace = fts_namespace_name(index_version.id)
        if index_version.fts_namespace != namespace:
            self._index_repo.set_fts_namespace(index_version.id, namespace)
        return namespace

    def _fts_namespace_by_id(self, index_version_id: str) -> str:
        """按索引版本 ID 解析 FTS 命名空间并保证已登记（幂等）"""
        index = self._index_repo.get(index_version_id)
        if index is None:
            raise EntityNotFoundError(f"索引版本不存在: {index_version_id}")
        return self._fts_namespace(index)

    @staticmethod
    def _embedded_cursor(checkpoint_json: str | None) -> int:
        """读取嵌入批次游标（已完成批次的最后一个切片序号；未开始为 -1）"""
        if not checkpoint_json:
            return -1
        payload = json.loads(checkpoint_json)
        cursor = payload.get("embedded_until") if isinstance(payload, dict) else None
        return cursor if isinstance(cursor, int) else -1

    @staticmethod
    def _embedding_progress(done_count: int, total: int) -> float:
        """嵌入批次进度：在切片完成与写入完成区间内按批线性推进"""
        if total == 0:
            return _EMBED_PROGRESS_BASE + _EMBED_PROGRESS_SPAN
        return round(
            _EMBED_PROGRESS_BASE + _EMBED_PROGRESS_SPAN * done_count / total, 4
        )

    def _require_gateway(self) -> EmbeddingGateway:
        """读取向量化网关；未配置密钥时按认证失败处理"""
        if self._embedding_gateway is None:
            raise EmbeddingAuthError("未配置向量化服务密钥")
        return self._embedding_gateway

    def _succeed(self, task_id: str) -> None:
        """任务成功收尾：进度置满并迁移终态；与取消请求竞争时按取消语义收尾"""
        self._set_stage(task_id, TaskStage.COMPLETED)
        try:
            self._task_repo.transition(
                task_id, TaskStatus.SUCCEEDED, stage=TaskStage.COMPLETED
            )
        except TaskStateConflictError:
            # 成功迁移被拒的唯一可能：取消请求先转入等待取消状态
            self._task_repo.cancel_at_checkpoint(task_id, self._worker_id)
        logger.info("任务完成 %s", task_id)

    def _checkpoint(self, task_id: str) -> None:
        """阶段边界检查：租约复核与取消检查，任一触发即终止本次处理

        租约复核借道心跳续约：租约有效则顺带延长，失效即抛错终止；
        取消请求则在此收尾为取消终态
        """
        task = self._task_repo.heartbeat(task_id, self._worker_id)
        if task.state is TaskStatus.CANCEL_REQUESTED:
            self._task_repo.cancel_at_checkpoint(task_id, self._worker_id)
            raise _TaskInterrupted()

    def _set_stage(self, task_id: str, stage: TaskStage) -> None:
        """推进处理阶段并写入该阶段的标准进度"""
        self._task_repo.update_stage(
            task_id,
            self._worker_id,
            stage=stage,
            progress=_STAGE_PROGRESS.get(stage),
        )

    def _require_version(self, task: Task) -> DocumentVersion:
        """读取任务引用的文档版本；缺失属内部事实破坏，直接失败"""
        version_ref = task.document_version_id
        if version_ref is None:
            raise RuntimeError(f"任务缺少文档版本引用: {task.id}")
        version = self._version_repo.get(version_ref)
        if version is None:
            raise RuntimeError(f"任务引用的文档版本不存在: {version_ref}")
        return version

    def _require_client(self) -> MinerUClient:
        """读取云端解析客户端；未配置令牌时按认证失败处理"""
        if self._mineru is None:
            raise CloudAuthError("未配置云端解析令牌")
        return self._mineru

    def _find_external(self, task_id: str) -> ExternalTask | None:
        """按任务读取批次事实（单文件任务对应单条记录）"""
        records = self._external_repo.list_by_task(task_id)
        return records[0] if records else None

    def _fail(
        self, task_id: str, error_code: str, message: str
    ) -> Task | None:
        """转入失败终态；租约已丢失时放弃写入，交由恢复流程接管

        :return: 失败后的任务（租约丢失写入被拒时为 None）
        """
        try:
            task = self._task_repo.fail_task(
                task_id,
                self._worker_id,
                error_code=error_code,
                error_message=message,
            )
            logger.warning("任务失败 %s: %s", task_id, error_code)
            return task
        except TaskLeaseLostError:
            logger.warning("任务失败写入被拒（租约丢失）%s", task_id)
            return None

    def _retry(
        self, task_id: str, error_code: str, message: str
    ) -> Task | None:
        """安排当前阶段的自动重试；租约已丢失时放弃写入

        :return: 安排后的任务（重试预算耗尽即为失败终态；租约丢失
            写入被拒时为 None）
        """
        try:
            task = self._task_repo.schedule_retry(
                task_id,
                self._worker_id,
                error_code=error_code,
                error_message=message,
            )
            logger.warning("任务安排自动重试 %s: %s", task_id, error_code)
            return task
        except TaskLeaseLostError:
            logger.warning("任务重试写入被拒（租约丢失）%s", task_id)
            return None
