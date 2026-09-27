# -*- coding: utf-8 -*-
"""FastAPI 服务主入口

装配工作台运行时（SQLite 业务事实源、任务化导入 Worker、查询链路与
评测编排），并暴露 /api/v1 接口供 Flutter 桌面端调用。
"""

# 导入系统模块
import logging
import os
import sys
import threading

# 导入时间模块（用于性能监控）
import time

# 导入 traceback 模块（用于打印完整异常堆栈）
import traceback

# 将脚本所在目录添加到 sys.path，确保同目录模块可正确导入
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# ===================== 日志配置 =====================

class ColorFormatter(logging.Formatter):
    """带颜色输出的日志格式化器（适配 Windows 终端）"""

    # 日志级别颜色映射
    LEVEL_COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "\033[36m",     # 青色
        logging.INFO: "\033[32m",      # 绿色
        logging.WARNING: "\033[33m",   # 黄色
        logging.ERROR: "\033[31m",     # 红色
        logging.CRITICAL: "\033[35m",  # 紫色
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录，添加颜色前缀"""
        color = self.LEVEL_COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname:<8}{self.RESET}"
        return super().format(record)


def setup_logging() -> logging.Logger:
    """
    初始化全局日志配置
    - 控制台输出带颜色的日志
    - 同时写入 logs/app.log 文件
    """
    log_format = "%(asctime)s │ %(levelname)s │ %(name)s │ %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logger = logging.getLogger("rag_api")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        return logger

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ColorFormatter(log_format, datefmt=date_format))
    logger.addHandler(console_handler)

    # 文件 handler
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.FileHandler(
        os.path.join(log_dir, "app.log"), encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    logger.addHandler(file_handler)

    return logger


# 全局 logger 实例
logger = setup_logging()

# 导入 dotenv 模块，用于从 .env 文件加载环境变量
from dotenv import load_dotenv

# ===================== 编码设置：强制使用 UTF-8 =====================
# 解决 Windows 下控制台默认 GBK 编码导致的中文乱码问题
os.environ['PYTHONIOENCODING'] = 'utf-8'

# 工作台运行时：SQLite 事实源 + 任务化导入（/api/v1）
import chromadb

from app.api.v1 import (
    ApiV1Dependencies,
    ConversationDependencies,
    DocumentDependencies,
    EvaluationDependencies,
    KnowledgeBaseDependencies,
    MetricsDependencies,
    OpsDependencies,
    QueryDependencies,
    SearchDependencies,
    TaskDependencies,
    create_api_router,
)
from app.api.v1.envelope import new_request_id, success_envelope
from app.domain.ids import uuid7
from app.infrastructure.embedding import (
    CachingQueryEmbedder,
    DashScopeEmbeddingGateway,
)
from app.infrastructure.evaluation import EvaluationRunService
from app.infrastructure.generation import build_generation_gateway
from app.infrastructure.ingest import ImportOrchestrator
from app.infrastructure.keywordindex import (
    JiebaTokenizer,
    SQLiteFtsKeywordIndex,
    load_jieba_userdict,
)
from app.infrastructure.mineru import MinerUClient
from app.infrastructure.model_settings import ModelSettings
from app.infrastructure.query import QueryOrchestrator
from app.infrastructure.rerank import DashScopeRerankGateway
from app.infrastructure.retrieval import ContextResolver, RetrievalService
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.migrations import apply_migrations
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteCitationRepository,
    SQLiteConfigRepository,
    SQLiteContentRepository,
    SQLiteConversationRepository,
    SQLiteDeletionRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteEmbeddingCacheRepository,
    SQLiteEvaluationRunRepository,
    SQLiteExternalTaskRepository,
    SQLiteIndexVersionRepository,
    SQLiteKnowledgeBaseRepository,
    SQLiteQueryEventStore,
    SQLiteQueryRunRepository,
    SQLiteSystemSettingsRepository,
)
from app.infrastructure.sqlite.repositories.import_repository import (
    SQLiteImportRepository,
)
from app.infrastructure.sqlite.repositories.task_repository import SQLiteTaskRepository
from app.infrastructure.storage.upload_staging import UploadStagingStore
from app.infrastructure.vectorindex import ChromaVectorIndexAdapter
from app.infrastructure.worker import ImportTaskWorker

# 从 .env 文件加载环境变量（API Key 等敏感信息不硬编码）
load_dotenv(os.path.join(BASE_DIR, ".env"))

# 模型身份与调用参数在装配期解析一次：网关、编排器与运维端点都从这里
# 取值，避免各自读环境变量导致口径漂移
MODEL_SETTINGS = ModelSettings.from_env()

# 从环境变量获取 DashScope API Key，如果没有则为空字符串
DASHSCOPE_API_KEY = MODEL_SETTINGS.dashscope_api_key

# ===================== FastAPI 应用初始化 =====================

app = FastAPI(
    title="RAG Document AI API",
    description="本机文档智能问答工作台：SQLite 事实源 + 派生索引 + 任务化导入",
    version="1.0.0"
)

# 添加 CORS 中间件，允许 Flutter 桌面端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源，桌面应用场景下可放宽
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有 HTTP 方法
    allow_headers=["*"],  # 允许所有请求头
)


# ===================== 工作台运行时（SQLite + 任务化导入） =====================

# 业务事实库与受控暂存目录：默认位于服务目录内，可用环境变量覆盖；
# 示例配置文件允许留空，空值与未设置同样回退默认
WORKBENCH_DB_PATH = (
    os.getenv("WORKBENCH_DB_PATH")
    or os.path.join(BASE_DIR, "data", "workbench.db")
)
WORKBENCH_STAGING_DIR = (
    os.getenv("WORKBENCH_STAGING_DIR")
    or os.path.join(BASE_DIR, "uploads", "staging")
)
# 派生向量索引的持久化目录：索引可由解析事实整体重建，目录可随索引
# 生命周期清理
WORKBENCH_CHROMA_DIR = (
    os.getenv("WORKBENCH_CHROMA_DIR")
    or os.path.join(BASE_DIR, "data", "chroma_workbench")
)
# 本地调试检索开关：显式开启后 /search 端点可用（Top-K 覆盖与候选
# 明细为调试能力），发布包默认关闭
WORKBENCH_LOCAL_DEBUG = (
    (os.getenv("WORKBENCH_LOCAL_DEBUG") or "0").strip().lower()
    in ("1", "true", "on")
)

# jieba 领域词典（可选）：把知识库专业术语注册为整词，修复默认词典
# 的领域词碎片化；文件缺失或无在役词条即使用默认词典。加载在启动
# 早期完成，查询预热与索引构建（Worker）的首次分词都在其后
_userdict_path = (
    os.getenv("WORKBENCH_JIEBA_USER_DICT")
    or os.path.join(BASE_DIR, "data", "jieba_userdict.txt")
)
_userdict_words = load_jieba_userdict(_userdict_path)
if _userdict_words:
    logger.info(f"jieba 领域词典已加载 {_userdict_words} 个词条")

os.makedirs(os.path.dirname(WORKBENCH_DB_PATH), exist_ok=True)

# 启动时应用数据库迁移（幂等，可重复执行），再建立连接与仓储
apply_migrations(WORKBENCH_DB_PATH, os.path.join(BASE_DIR, "migrations"))
_workbench_conn = connect(WORKBENCH_DB_PATH)
_workbench_orchestrator = ImportOrchestrator(
    staging_store=UploadStagingStore(WORKBENCH_STAGING_DIR),
    import_repo=SQLiteImportRepository(_workbench_conn),
)
_workbench_task_repo = SQLiteTaskRepository(_workbench_conn)

# 查询链路装配：实时问答编排（检索→重排→组装→流式生成→引用快照）。
# 启动恢复把进程重启残留的未完成查询置为失败（与任务恢复语义对齐）
_query_run_repo = SQLiteQueryRunRepository(_workbench_conn)
_query_event_store = SQLiteQueryEventStore(_workbench_conn)
_query_conversation_repo = SQLiteConversationRepository(_workbench_conn)
_query_citation_repo = SQLiteCitationRepository(_workbench_conn)
_query_config_repo = SQLiteConfigRepository(_workbench_conn)
_query_kb_repo = SQLiteKnowledgeBaseRepository(_workbench_conn)
_query_dashscope_key = MODEL_SETTINGS.dashscope_api_key
# 查询侧向量化网关：真实网关供启动连接预热直连使用；检索服务用
# 缓存装饰后的实例（模型 + 查询侧别 + 问题哈希为键），评测编排同
# 问题集多参数组执行与线上重复提问直接复用向量
_real_query_embedder = (
    DashScopeEmbeddingGateway(api_key=_query_dashscope_key)
    if _query_dashscope_key
    else None
)
_query_embedder = (
    CachingQueryEmbedder(
        gateway=_real_query_embedder,
        cache=SQLiteEmbeddingCacheRepository(_workbench_conn),
    )
    if _real_query_embedder is not None
    else None
)
# 检索服务与事实解析器为查询链路与调试检索共享的单例（同一实例保证
# 调试观察到的检索行为与生产一致）
_text_tokenizer = JiebaTokenizer()
_retrieval_service = RetrievalService(
    index_repo=SQLiteIndexVersionRepository(_workbench_conn),
    chunk_repo=SQLiteChunkRepository(_workbench_conn),
    query_embedder=_query_embedder,
    vector_index=ChromaVectorIndexAdapter(
        chromadb.PersistentClient(path=WORKBENCH_CHROMA_DIR)
    ),
    keyword_index=SQLiteFtsKeywordIndex(_workbench_conn),
    tokenizer=_text_tokenizer,
)
_context_resolver = ContextResolver(
    index_repo=SQLiteIndexVersionRepository(_workbench_conn),
    chunk_repo=SQLiteChunkRepository(_workbench_conn),
    document_repo=SQLiteDocumentRepository(_workbench_conn),
    version_repo=SQLiteDocumentVersionRepository(_workbench_conn),
)


def _build_rerank_gateway() -> DashScopeRerankGateway | None:
    """重排网关：重排由云端供应方承载，云端凭据缺失时为未配置"""
    if not MODEL_SETTINGS.rerank_available:
        return None
    return DashScopeRerankGateway(
        api_key=MODEL_SETTINGS.dashscope_api_key,
        model=MODEL_SETTINGS.rerank_model,
    )


def _model_profiles() -> list[dict[str, str]]:
    """运维端点展示的生效模型身份：与网关注入参数同源"""
    return [
        {
            "role": "embedding",
            "provider": "dashscope",
            "model_name": MODEL_SETTINGS.embedding_model,
        },
        {
            "role": "rerank",
            "provider": "dashscope",
            "model_name": MODEL_SETTINGS.rerank_model,
        },
        {
            "role": "generation",
            "provider": MODEL_SETTINGS.generation_provider,
            "model_name": MODEL_SETTINGS.generation_model,
        },
    ]


_query_deps = QueryDependencies(
    orchestrator=QueryOrchestrator(
        run_repo=_query_run_repo,
        event_store=_query_event_store,
        conversation_repo=_query_conversation_repo,
        config_repo=_query_config_repo,
        citation_repo=_query_citation_repo,
        retrieval_service=_retrieval_service,
        resolver=_context_resolver,
        rerank_gateway=_build_rerank_gateway(),
        generation_gateway=build_generation_gateway(MODEL_SETTINGS),
        model_settings=MODEL_SETTINGS,
    ),
    run_repo=_query_run_repo,
    kb_repo=_query_kb_repo,
    conversation_repo=_query_conversation_repo,
    citation_repo=_query_citation_repo,
)
try:
    _interrupted = _query_deps.orchestrator.fail_interrupted()
    if _interrupted:
        print(f"[workbench] 恢复：{_interrupted} 个中断查询已置为失败")
except Exception as _recover_error:  # noqa: BLE001 - 恢复失败不阻断启动
    print(f"[workbench] 查询恢复失败: {type(_recover_error).__name__}")


# 查询路径预热：冷启动首题需支付两笔一次性成本——DashScope 的
# DNS/TCP/TLS 建连（实测约 0.7s）与 jieba 词典惰性加载（实测约
# 0.7s），直接计入首题检索段。后台线程经查询侧网关与分词器单例各
# 发一笔最小调用完成预热；无凭据时仍预热本地分词器，预热失败仅
# 记录（首题退化为普通冷启动延迟，查询链路本身不受影响）
def _start_query_warmup() -> None:
    def _run() -> None:
        started = time.monotonic()
        try:
            _text_tokenizer.tokenize(["预热"])
            # 连接预热绕过查询缓存直连真实网关：缓存命中不发起网络
            # 请求，预热就失去建连意义
            if _real_query_embedder is not None:
                _real_query_embedder.embed_query("预热")
        except Exception as exc:  # noqa: BLE001 - 预热失败不阻断启动
            logger.warning(
                f"查询路径预热失败: {type(exc).__name__}"
                "（首题将支付冷启动成本）"
            )
            return
        logger.info(
            "查询路径预热完成，"
            f"耗时 {int((time.monotonic() - started) * 1000)}ms"
        )

    threading.Thread(target=_run, daemon=True, name="query-warmup").start()


_start_query_warmup()


# ===================== 评测编排桥接 =====================

_evaluation_run_repo = SQLiteEvaluationRunRepository(_workbench_conn)
_evaluation_settings_repo = SQLiteSystemSettingsRepository(_workbench_conn)


def _kb_active_version_ids(kb_id: str) -> list[str]:
    """知识库当前全部活动文档版本（评测参评集合快照）"""
    rows = _workbench_conn.execute(
        "SELECT dv.id FROM document_versions dv"
        " JOIN documents d ON d.id = dv.document_id"
        " WHERE d.knowledge_base_id = ?"
        "   AND dv.id = d.active_document_version_id"
        " ORDER BY dv.id",
        (kb_id,),
    ).fetchall()
    return [row[0] for row in rows]


def _launch_evaluation_query(kb_id: str, question: str) -> str:
    """评测问题经真实查询链路执行（指标事实照常落库）"""
    start = _query_deps.orchestrator.start_query(kb_id=kb_id, question=question)
    return start.run.id


def _read_query_metrics(run_id: str) -> dict:
    """查询运行的评测聚合口径行（与查询指标事实同源）"""
    row = _workbench_conn.execute(
        "SELECT state, refused, rerank_degraded, server_ttft_ms,"
        " input_tokens, output_tokens FROM query_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return {
            "state": "failed",
            "refused": 0,
            "rerank_degraded": 0,
            "server_ttft_ms": None,
            "input_tokens": None,
            "output_tokens": None,
        }
    return {
        "state": row[0],
        "refused": bool(row[1]),
        "rerank_degraded": bool(row[2]),
        "server_ttft_ms": row[3],
        "input_tokens": row[4],
        "output_tokens": row[5],
    }


_evaluation_service = EvaluationRunService(
    evaluation_repo=_evaluation_run_repo,
    settings_repo=_evaluation_settings_repo,
    content_repo=SQLiteContentRepository(_workbench_conn),
    version_ids_reader=_kb_active_version_ids,
    query_launcher=_launch_evaluation_query,
    query_metrics_reader=_read_query_metrics,
)

# 解析 Worker：单线程消费导入任务（解析结果落库的唯一写入方）。
# 默认启用，WORKBENCH_WORKER_ENABLED 设为 0/false/off 关闭；关闭时
# 导入任务停留在队列，由下次启动的恢复流程继续推进。云端解析令牌
# 缺失时 Worker 仍消费本地路线，云端任务在提交时按认证失败处理。
# 评测编排列为可选依赖（评测服务与运行仓储，随查询链路装配就绪）
_import_worker: ImportTaskWorker | None = None
_worker_thread: threading.Thread | None = None
if (os.getenv("WORKBENCH_WORKER_ENABLED") or "1").strip().lower() not in ("0", "false", "off"):
    _mineru_token = os.getenv("MINERU_API_TOKEN") or ""
    _dashscope_key = os.getenv("DASHSCOPE_API_KEY") or ""
    _import_worker = ImportTaskWorker(
        task_repo=_workbench_task_repo,
        document_repo=SQLiteDocumentRepository(_workbench_conn),
        version_repo=SQLiteDocumentVersionRepository(_workbench_conn),
        content_repo=SQLiteContentRepository(_workbench_conn),
        external_repo=SQLiteExternalTaskRepository(_workbench_conn),
        index_repo=SQLiteIndexVersionRepository(_workbench_conn),
        chunk_repo=SQLiteChunkRepository(_workbench_conn),
        config_repo=SQLiteConfigRepository(_workbench_conn),
        settings_repo=SQLiteSystemSettingsRepository(_workbench_conn),
        embedding_cache=SQLiteEmbeddingCacheRepository(_workbench_conn),
        evaluation_repo=_evaluation_run_repo,
        evaluation_service=_evaluation_service,
        embedding_gateway=(
            DashScopeEmbeddingGateway(api_key=_dashscope_key) if _dashscope_key else None
        ),
        vector_index=ChromaVectorIndexAdapter(
            chromadb.PersistentClient(path=WORKBENCH_CHROMA_DIR)
        ),
        text_tokenizer=JiebaTokenizer(),
        keyword_index=SQLiteFtsKeywordIndex(_workbench_conn),
        mineru_client=MinerUClient(api_token=_mineru_token) if _mineru_token else None,
        work_dir=os.path.join(WORKBENCH_STAGING_DIR, "cloud_results"),
        worker_id=f"worker-{uuid7()}",
    )
    # 线程引用供健康探针判断工作循环存活
    _worker_thread = _import_worker.start_background()

    @app.on_event("shutdown")
    def _stop_import_worker() -> None:
        """应用关闭时请求 Worker 停止：进行中的轮询在下一个等待间隔内退出"""
        if _import_worker is not None:
            _import_worker.stop()

app.include_router(
    create_api_router(
        ApiV1Dependencies(
            orchestrator=_workbench_orchestrator,
            task_repo=_workbench_task_repo,
            query=_query_deps,
            metrics=MetricsDependencies(
                run_repo=_query_run_repo, conn=_workbench_conn
            ),
            knowledge_bases=KnowledgeBaseDependencies(
                kb_repo=SQLiteKnowledgeBaseRepository(_workbench_conn),
                deletion_repo=SQLiteDeletionRepository(_workbench_conn),
                task_repo=_workbench_task_repo,
            ),
            documents=DocumentDependencies(
                orchestrator=_workbench_orchestrator,
                kb_repo=SQLiteKnowledgeBaseRepository(_workbench_conn),
                document_repo=SQLiteDocumentRepository(_workbench_conn),
                version_repo=SQLiteDocumentVersionRepository(_workbench_conn),
                index_repo=SQLiteIndexVersionRepository(_workbench_conn),
                content_repo=SQLiteContentRepository(_workbench_conn),
                task_repo=_workbench_task_repo,
                deletion_repo=SQLiteDeletionRepository(_workbench_conn),
                import_repo=SQLiteImportRepository(_workbench_conn),
            ),
            tasks=TaskDependencies(
                task_repo=_workbench_task_repo,
                document_repo=SQLiteDocumentRepository(_workbench_conn),
                version_repo=SQLiteDocumentVersionRepository(_workbench_conn),
            ),
            conversations=ConversationDependencies(
                kb_repo=SQLiteKnowledgeBaseRepository(_workbench_conn),
                conversation_repo=_query_conversation_repo,
                citation_repo=_query_citation_repo,
            ),
            ops=OpsDependencies(
                conn=_workbench_conn,
                chroma_client=chromadb.PersistentClient(path=WORKBENCH_CHROMA_DIR),
                worker_enabled=_import_worker is not None,
                worker_alive=lambda: (
                    _worker_thread is not None and _worker_thread.is_alive()
                ),
                mineru_configured=bool(os.getenv("MINERU_API_TOKEN")),
                dashscope_configured=bool(os.getenv("DASHSCOPE_API_KEY")),
                local_debug_enabled=WORKBENCH_LOCAL_DEBUG,
                settings_repo=SQLiteSystemSettingsRepository(_workbench_conn),
                model_profiles=_model_profiles(),
            ),
            search=SearchDependencies(
                kb_repo=_query_kb_repo,
                retrieval_service=_retrieval_service,
                resolver=_context_resolver,
                rerank_gateway=_build_rerank_gateway(),
                local_debug_enabled=WORKBENCH_LOCAL_DEBUG,
            ),
            evaluations=EvaluationDependencies(
                conn=_workbench_conn,
                kb_repo=SQLiteKnowledgeBaseRepository(_workbench_conn),
                task_repo=_workbench_task_repo,
                evaluation_repo=_evaluation_run_repo,
                evaluation_service=_evaluation_service,
            ),
        )
    )
)


# ===================== 请求/响应日志中间件 =====================

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    HTTP 请求/响应日志中间件
    自动记录每个请求的方法、路径、耗时和响应状态码
    """
    # 跳过健康检查和文档接口，避免刷屏
    skip_paths = {"/ping", "/docs", "/openapi.json", "/redoc"}
    if request.url.path in skip_paths:
        return await call_next(request)

    start_time = time.time()
    method = request.method
    path = request.url.path

    # 构造请求摘要（含查询参数）
    query_str = str(request.query_params) if request.query_params else ""
    request_summary = f"{method} {path}"
    if query_str:
        request_summary += f"?{query_str}"

    logger.info(f"→ {request_summary}")

    try:
        response = await call_next(request)
    except Exception as exc:
        # 记录未捕获的异常
        elapsed = round((time.time() - start_time) * 1000, 1)
        logger.error(
            f"✗ {request_summary} │ {elapsed}ms │ 500 Internal Server Error │ {type(exc).__name__}: {exc}"
        )
        logger.debug(traceback.format_exc())
        raise

    elapsed = round((time.time() - start_time) * 1000, 1)
    status_code = response.status_code

    # 根据状态码选择日志级别
    if status_code >= 500:
        logger.error(f"✗ {request_summary} │ {elapsed}ms │ {status_code}")
    elif status_code >= 400:
        logger.warning(f"⚠ {request_summary} │ {elapsed}ms │ {status_code}")
    else:
        logger.info(f"← {request_summary} │ {elapsed}ms │ {status_code}")

    return response


# ===================== 存活探针 =====================

@app.get("/ping", tags=["系统"])
async def ping():
    """存活探针：桌面端连接指示灯据此判断服务可达性"""
    return success_envelope({"status": "pong"}, new_request_id())


# ===================== 服务启动入口 =====================

if __name__ == "__main__":
    import uvicorn
    # 启动 FastAPI 服务，监听本地 8000 端口
    logger.info("=" * 60)
    logger.info("RAG Document AI API 服务启动中...")
    logger.info(f"业务事实库: {WORKBENCH_DB_PATH}")
    logger.info(f"向量索引目录: {WORKBENCH_CHROMA_DIR}")
    logger.info("=" * 60)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        timeout_keep_alive=300,
    )