# -*- coding: utf-8 -*-
"""
FastAPI RAG 服务主入口
基于原 rag_main.py 的 RAG 业务逻辑，封装为 HTTP REST API 服务
供 Flutter 桌面端通过 HTTP 协议调用
"""

# 导入系统模块
import os
import sys
# 导入 IO 模块，用于重新设置标准流的编码
import io
# 导入 JSON 模块
import json
# 导入时间模块（用于性能监控）
import time
# 导入日志模块
import logging
# 导入 traceback 模块（用于打印完整异常堆栈）
import traceback
# 导入 pathlib 模块（用于路径处理）
from pathlib import Path

# 将脚本所在目录添加到 sys.path，确保同目录模块可正确导入
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 导入 FastAPI 相关模块
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union


# ===================== 日志配置 =====================

class ColorFormatter(logging.Formatter):
    """带颜色输出的日志格式化器（适配 Windows 终端）"""

    # 日志级别颜色映射
    LEVEL_COLORS = {
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


# ===================== 统一错误码定义 =====================

class ErrorCode:
    """API 错误码常量"""
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    EMPTY_FILE = "EMPTY_FILE"
    API_ERROR = "API_ERROR"
    DB_ERROR = "DB_ERROR"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"
    INVALID_PARAM = "INVALID_PARAM"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


# ===================== 统一响应格式 =====================

def make_response(
    success: bool,
    data: Any = None,
    error_code: Optional[str] = None,
    error_msg: Optional[str] = None
) -> Dict[str, Any]:
    """
    构造统一 API 响应格式
    :param success: 是否成功
    :param data: 成功时的数据
    :param error_code: 错误码（失败时必填）
    :param error_msg: 错误信息（失败时必填）
    :return: 统一格式的字典
    """
    response = {"success": success}
    if success:
        response["data"] = data
        response["error"] = None
    else:
        response["data"] = None
        response["error"] = {
            "code": error_code or ErrorCode.UNKNOWN,
            "message": error_msg or "未知错误"
        }
    return response


def success_response(data: Any = None) -> Dict[str, Any]:
    """构造成功响应"""
    return make_response(success=True, data=data)


def error_response(
    error_code: str = ErrorCode.UNKNOWN,
    error_msg: str = "未知错误"
) -> Dict[str, Any]:
    """构造失败响应"""
    return make_response(success=False, error_code=error_code, error_msg=error_msg)

# 导入 dotenv 模块，用于从 .env 文件加载环境变量
from dotenv import load_dotenv

# ===================== 编码设置：强制使用 UTF-8 =====================
# 解决 Windows 下控制台默认 GBK 编码导致的中文乱码问题
os.environ['PYTHONIOENCODING'] = 'utf-8'

# 导入 DashScope（阿里云 Qwen 服务）SDK，用于调用 Qwen 的 Embedding 和 LLM 接口
import dashscope
# 从 DashScope 导入文本嵌入类
from dashscope import TextEmbedding

# 导入 LangChain 的文本分割器，用于将长文本切分为小块
from langchain_text_splitters import RecursiveCharacterTextSplitter
# 导入 LangChain 的文档加载器，用于加载文本文件
from langchain_community.document_loaders import TextLoader
# 导入 LangChain 的 Document 类，用于构造文档对象
from langchain_core.documents import Document
# 导入 LangChain 的 Chroma 向量数据库封装
from langchain_chroma import Chroma
# 导入 LangChain 的 Embeddings 基类，用于自定义 Embedding
from langchain_core.embeddings import Embeddings
# 导入自定义文件解析器（支持 TXT/DOCX/PDF）
from file_parser import parse_file, SUPPORTED_EXTENSIONS as PARSER_SUPPORTED_EXTENSIONS
# 工作台 v1 运行时：SQLite 事实源 + 任务化导入（/api/v1）
from app.api.v1 import ApiV1Dependencies, create_api_router
from app.domain.ids import uuid7
from app.infrastructure.ingest import ImportOrchestrator
from app.infrastructure.mineru import MinerUClient
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.migrations import apply_migrations
from app.infrastructure.sqlite.repositories.import_repository import SQLiteImportRepository
from app.infrastructure.sqlite.repositories import (
    SQLiteContentRepository,
    SQLiteDocumentRepository,
    SQLiteDocumentVersionRepository,
    SQLiteExternalTaskRepository,
)
from app.infrastructure.sqlite.repositories.task_repository import SQLiteTaskRepository
from app.infrastructure.storage.upload_staging import UploadStagingStore
from app.infrastructure.worker import ImportTaskWorker
# 导入 LLM 封装类（支持网络模型和本地 Ollama 模型）
from llm_wrapper import LLMWrapper, LLMProviderType, DEFAULT_NETWORK_MODEL, DEFAULT_LOCAL_MODEL
# 导入 LangChain 的提示词模板，用于构建对话提示
from langchain_core.prompts import ChatPromptTemplate
# 导入 LangChain 的可运行组件，用于构建 RAG 管道
from langchain_core.runnables import RunnablePassthrough
# 导入 LangChain 的输出解析器，用于解析 LLM 输出
from langchain_core.output_parsers import StrOutputParser

# ===================== 环境变量与路径配置 =====================

# ChromaDB 向量数据库持久化存储目录
CHROMA_DIR = os.path.join(BASE_DIR, "chroma_db")
# 知识库文本文件路径
DOC_PATH = os.path.join(BASE_DIR, "docs", "knowledge.txt")
# 临时上传文件目录
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

# 确保上传目录存在
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 从 .env 文件加载环境变量（API Key 等敏感信息不硬编码）
load_dotenv(os.path.join(BASE_DIR, ".env"))

# 从环境变量获取 DashScope API Key，如果没有则为空字符串
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
# 设置 DashScope 全局 API Key
dashscope.api_key = DASHSCOPE_API_KEY

# Embedding 模型名称，使用 Qwen 的文本向量模型
# 可选: text-embedding-v1, text-embedding-v2, text-embedding-v3
EMBEDDING_MODEL = "text-embedding-v2"
# 网络 LLM 模型名称
LLM_MODEL = "qwen-turbo"
# Qwen 的 OpenAI 兼容接口地址
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 本地 Ollama 服务地址
OLLAMA_BASE_URL = "http://localhost:11434/v1"
# 本地 Ollama 模型名称
OLLAMA_MODEL = "qwen3.5:4b"

# 可用模型列表（供前端选择）
AVAILABLE_MODELS = [
    {"id": "qwen-turbo", "name": "Qwen Turbo (网络)", "provider": "network", "base_url": QWEN_BASE_URL, "api_key": True},
    {"id": "qwen-plus", "name": "Qwen Plus (网络)", "provider": "network", "base_url": QWEN_BASE_URL, "api_key": True},
    {"id": f"ollama:{OLLAMA_MODEL}", "name": f"{OLLAMA_MODEL} (本地)", "provider": "local", "base_url": OLLAMA_BASE_URL, "api_key": False},
]

# ===================== 自定义 Qwen Embedding 类 =====================

class QwenEmbeddings(Embeddings):
    """
    Qwen 文本嵌入模型封装类
    实现 LangChain Embeddings 接口，可直接用于 ChromaDB 等组件
    """

    def __init__(self, model_name: str = EMBEDDING_MODEL):
        self.model_name = model_name

    def embed_documents(self, texts):
        """
        批量生成文本的嵌入向量
        :param texts: 文本列表
        :return: 向量列表（每个向量是一个浮点数列表）
        """
        response = TextEmbedding.call(
            model=self.model_name,
            input=texts
        )
        
        if response.status_code == 200:
            embeddings = [
                item["embedding"]
                for item in sorted(response.output["embeddings"], key=lambda x: x["text_index"])
            ]
            return embeddings
        else:
            raise Exception(f"Qwen Embedding 调用失败: {response.code} - {response.message}")

    def embed_query(self, text):
        """
        生成单个查询文本的嵌入向量
        :param text: 查询文本
        :return: 向量（浮点数列表）
        """
        return self.embed_documents([text])[0]

# ===================== ChromaDB 向量数据库操作类 =====================

class ChromaDBManager:
    """
    ChromaDB 向量数据库管理类
    封装增、删、改、查等常用操作
    """

    def __init__(self, persist_directory: str, embedding_function):
        self.persist_directory = persist_directory
        self.embedding_function = embedding_function
        self.collection_name = "knowledge_base"
        self.vector_store = self._init_vector_store()

    def _init_vector_store(self):
        """
        初始化 Chroma 向量存储
        如果目录已存在则加载，否则创建空的向量库
        """
        if os.path.exists(self.persist_directory) and os.listdir(self.persist_directory):
            db = Chroma(
                persist_directory=self.persist_directory,
                embedding_function=self.embedding_function,
                collection_name=self.collection_name
            )
            return db
        else:
            db = Chroma(
                persist_directory=self.persist_directory,
                embedding_function=self.embedding_function,
                collection_name=self.collection_name
            )
            return db

    # ===================== 增：添加文档 =====================

    def add_texts(self, texts, metadatas=None):
        ids = self.vector_store.add_texts(
            texts=texts,
            metadatas=metadatas
        )
        return ids

    def add_documents(self, documents):
        ids = self.vector_store.add_documents(documents=documents)
        return ids

    # ===================== 删：删除文档 =====================

    def delete_by_ids(self, ids):
        self.vector_store.delete(ids=ids)

    def delete_by_source(self, source):
        """
        根据源文件路径删除关联的所有文档
        :param source: 文件路径（metadata 中的 source_path 或 source 字段）
        :return: 删除的文档数量
        """
        all_data = self.vector_store.get()
        all_ids = all_data.get("ids", [])
        all_metadatas = all_data.get("metadatas", [])
        ids_to_delete = []
        for doc_id, metadata in zip(all_ids, all_metadatas):
            if metadata and (metadata.get("source_path") == source or metadata.get("source") == source):
                ids_to_delete.append(doc_id)
        if ids_to_delete:
            self.vector_store.delete(ids=ids_to_delete)
        return len(ids_to_delete)

    def delete_all(self):
        all_ids = self.vector_store.get()["ids"]
        if all_ids:
            self.vector_store.delete(ids=all_ids)

    # ===================== 改：更新文档 =====================

    def update_text(self, doc_id, text, metadata=None):
        self.vector_store.delete(ids=[doc_id])
        self.vector_store.add_texts(
            texts=[text],
            metadatas=[metadata] if metadata else None,
            ids=[doc_id]
        )

    # ===================== 查：相似度检索 =====================

    def similarity_search(self, query, k=3):
        docs = self.vector_store.similarity_search(
            query=query,
            k=k
        )
        return docs

    def similarity_search_with_score(self, query, k=3):
        docs_with_scores = self.vector_store.similarity_search_with_score(
            query=query,
            k=k
        )
        return docs_with_scores

    def as_retriever(self, k=3):
        retriever = self.vector_store.as_retriever(
            search_kwargs={"k": k}
        )
        return retriever

    # ===================== 其他工具方法 =====================

    def count(self):
        return len(self.vector_store.get()["ids"])

    def get_all(self):
        result = self.vector_store.get()
        return result

# ===================== 知识库加载与构建 =====================

def load_knowledge_file(file_path: str):
    """
    加载并切分知识库文件
    支持 TXT/DOCX/PDF（通过 file_parser 解析）以及其他纯文本格式（通过 TextLoader）
    :param file_path: 知识库文件路径
    :return: 切分后的 Document 对象列表
    :raises: FileNotFoundError, ValueError, Exception
    """
    # 检查文件是否存在
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    # 检查文件是否为空
    if os.path.getsize(file_path) == 0:
        raise ValueError("文件内容为空")

    ext = Path(file_path).suffix.lower()

    # 使用 file_parser 处理 TXT/DOCX/PDF 格式
    if ext in PARSER_SUPPORTED_EXTENSIONS:
        try:
            text = parse_file(file_path)
        except FileNotFoundError:
            raise
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"文件读取失败: {str(e)}")

        docs = [Document(page_content=text, metadata={"source": file_path})]
    else:
        # 其他文本格式（.md/.py/.java 等）仍使用 TextLoader
        if ext not in ['.md', '.py', '.java', '.js', '.json', '.csv', '.log', '']:
            raise ValueError(f"不支持的文件格式: {ext}")

        try:
            loader = TextLoader(file_path, encoding="utf-8")
            docs = loader.load()
        except UnicodeDecodeError:
            # 尝试 gbk 编码
            try:
                loader = TextLoader(file_path, encoding="gbk")
                docs = loader.load()
            except Exception:
                raise ValueError("无法解析文件编码，请使用 UTF-8 或 GBK 编码的文本文件")
        except Exception as e:
            raise ValueError(f"文件读取失败: {str(e)}")

    # 检查文档内容是否为空
    if not docs or all(len(doc.page_content.strip()) == 0 for doc in docs):
        raise ValueError("文件内容为空")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=300,
        chunk_overlap=50,
        separators=["\n\n", "\n", "。", "，", " ", ""]
    )

    splits = splitter.split_documents(docs)
    if not splits:
        raise ValueError("文件内容切分后为空")

    return splits

def build_knowledge_base(db_manager: ChromaDBManager, knowledge_file: str):
    """
    从知识库文件构建向量数据库
    如果向量库为空，则从文件加载并构建
    """
    if db_manager.count() == 0:
        if os.path.exists(knowledge_file):
            splits = load_knowledge_file(knowledge_file)
            db_manager.add_documents(splits)
            print(f"知识库构建完成，共 {len(splits)} 个文档块", file=sys.stderr)
        else:
            print(f"警告: 知识库文件 {knowledge_file} 不存在", file=sys.stderr)
    else:
        print(f"向量库已存在，共 {db_manager.count()} 个文档块", file=sys.stderr)

# ===================== 全局实例初始化（服务启动时执行一次） =====================

# 初始化自定义的 Qwen Embedding 实例
embeddings = QwenEmbeddings(model_name=EMBEDDING_MODEL)

# 初始化 LLM 封装实例（默认使用网络模型）
llm_wrapper = LLMWrapper(
    provider=LLMProviderType.NETWORK,
    model=LLM_MODEL,
    base_url=QWEN_BASE_URL,
    api_key=DASHSCOPE_API_KEY,
    temperature=0
)

# 创建 ChromaDB 管理器实例
db_manager = ChromaDBManager(
    persist_directory=CHROMA_DIR,
    embedding_function=embeddings
)

# 从知识库文件构建向量数据库（如果为空）
build_knowledge_base(db_manager, DOC_PATH)

# 获取检索器（返回最相关的 3 个文档）
retriever = db_manager.as_retriever(k=3)


# ===================== RAG 问答封装 =====================

def run_rag_with_sources(question: str, model_id: Optional[str] = None) -> Dict[str, Any]:
    """
    执行 RAG 问答，同时返回答案、检索到的源文档和耗时统计
    :param question: 用户问题
    :param model_id: 模型 ID（可选，默认使用当前模型）
    :return: {"answer": "...", "sources": [...], "meta": {"time": ..., "tokens": ..., "model": ...}}
    """
    total_start = time.time()

    # 切换模型（如果指定）
    if model_id:
        model_config = _find_model_config(model_id)
        if model_config:
            api_key = DASHSCOPE_API_KEY if model_config.get("api_key") else None
            llm_wrapper.switch_model(
                provider=model_config["provider"],
                model=_extract_model_name(model_id, model_config),
                base_url=model_config["base_url"],
                api_key=api_key,
            )

    # 阶段 1: 检索
    retrieve_start = time.time()
    source_docs = retriever.invoke(question)
    retrieve_time = round(time.time() - retrieve_start, 2)

    # 构造上下文
    context = "\n\n".join([doc.page_content for doc in source_docs])

    # 构造完整提示词（将系统指令、上下文、问题合并）
    prompt = f"""你是一个专业的文档助手，请根据下面的上下文回答用户的问题。要求：
    1. 如果上下文包含与问题相关的信息，请优先使用上下文信息回答，不要编造内容；
    2. 如果上下文中没有相关答案，可以使用你自身的知识回答；
    3. 回答要简洁、准确、有条理。
    
    <context>
    {context}
    </context>

    用户问题：{question}
"""

    # 阶段 2: LLM 生成
    llm_result = llm_wrapper.single_turn(prompt)
    answer = llm_result["text"]
    llm_meta = llm_result["meta"]

    # 总耗时
    total_time = round(time.time() - total_start, 2)

    return {
        "answer": answer,
        "sources": [doc.page_content for doc in source_docs],
        "meta": {
            "time": total_time,
            "retrieve_time": retrieve_time,
            "llm_time": llm_meta.get("time", 0),
            "tokens": llm_meta.get("tokens", 0),
            "model": llm_wrapper.model,
            "provider": llm_wrapper.provider,
        },
    }


def _find_model_config(model_id: str) -> Optional[Dict[str, Any]]:
    """根据模型 ID 查找配置"""
    for m in AVAILABLE_MODELS:
        if m["id"] == model_id:
            return m
    return None


def _extract_model_name(model_id: str, config: Dict[str, Any]) -> str:
    """从模型 ID 中提取实际模型名"""
    if model_id.startswith("ollama:"):
        return model_id[len("ollama:"):]
    return model_id

# ===================== FastAPI 请求/响应模型 =====================

class QueryRequest(BaseModel):
    """RAG 问答请求模型"""
    question: str = Field(..., description="用户的问题")
    model_id: Optional[str] = Field(default=None, description="模型 ID（可选，不指定则使用默认模型）")

class SearchRequest(BaseModel):
    """相似度检索请求模型"""
    query: str = Field(..., description="查询文本")
    k: int = Field(default=3, description="返回结果数量")

class AddFileRequest(BaseModel):
    """添加文件请求模型（通过本地路径）"""
    file_path: str = Field(..., description="文件的本地绝对路径")

class AddTextsRequest(BaseModel):
    """添加文本请求模型"""
    texts: List[str] = Field(..., description="文本列表")
    metadatas: Optional[List[Dict[str, Any]]] = Field(default=None, description="元数据列表")

class DeleteByIdsRequest(BaseModel):
    """按ID删除请求模型"""
    ids: List[str] = Field(..., description="文档ID列表")

class DeleteBySourceRequest(BaseModel):
    """按源文件路径删除请求模型"""
    source: str = Field(..., description="源文件路径（metadata 中的 source 字段）")

class UpdateTextRequest(BaseModel):
    """更新文档请求模型"""
    id: str = Field(..., description="文档ID")
    text: str = Field(..., description="新的文本内容")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="新的元数据")

# ===================== FastAPI 应用初始化 =====================

app = FastAPI(
    title="RAG Document AI API",
    description="基于 LangChain + ChromaDB + Qwen 的文档智能问答服务",
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

os.makedirs(os.path.dirname(WORKBENCH_DB_PATH), exist_ok=True)

# 启动时应用数据库迁移（幂等，可重复执行），再建立连接与仓储
apply_migrations(WORKBENCH_DB_PATH, os.path.join(BASE_DIR, "migrations"))
_workbench_conn = connect(WORKBENCH_DB_PATH)
_workbench_orchestrator = ImportOrchestrator(
    staging_store=UploadStagingStore(WORKBENCH_STAGING_DIR),
    import_repo=SQLiteImportRepository(_workbench_conn),
)
_workbench_task_repo = SQLiteTaskRepository(_workbench_conn)

# 解析 Worker：单线程消费导入任务（解析结果落库的唯一写入方）。
# 默认启用，WORKBENCH_WORKER_ENABLED 设为 0/false/off 关闭；关闭时
# 导入任务停留在队列，由下次启动的恢复流程继续推进。云端解析令牌
# 缺失时 Worker 仍消费本地路线，云端任务在提交时按认证失败处理
_import_worker: ImportTaskWorker | None = None
if (os.getenv("WORKBENCH_WORKER_ENABLED") or "1").strip().lower() not in ("0", "false", "off"):
    _mineru_token = os.getenv("MINERU_API_TOKEN") or ""
    _import_worker = ImportTaskWorker(
        task_repo=_workbench_task_repo,
        document_repo=SQLiteDocumentRepository(_workbench_conn),
        version_repo=SQLiteDocumentVersionRepository(_workbench_conn),
        content_repo=SQLiteContentRepository(_workbench_conn),
        external_repo=SQLiteExternalTaskRepository(_workbench_conn),
        mineru_client=MinerUClient(api_token=_mineru_token) if _mineru_token else None,
        work_dir=os.path.join(WORKBENCH_STAGING_DIR, "cloud_results"),
        worker_id=f"worker-{uuid7()}",
    )
    _import_worker.start_background()

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

# ===================== API 路由 =====================

# ===================== 异常处理辅助函数 =====================

def _handle_exception(e: Exception) -> Dict[str, Any]:
    """
    将异常转换为统一错误响应，同时记录错误日志和完整堆栈
    """
    # 记录完整异常堆栈（DEBUG 级别写入文件）
    logger.debug(traceback.format_exc())

    if isinstance(e, FileNotFoundError):
        logger.error(f"文件未找到: {e}")
        return error_response(ErrorCode.FILE_NOT_FOUND, str(e))
    elif isinstance(e, ValueError):
        msg = str(e).lower()
        if "不支持的文件格式" in str(e) or "unsupported" in msg:
            logger.warning(f"不支持的文件格式: {e}")
            return error_response(ErrorCode.UNSUPPORTED_FORMAT, str(e))
        elif "空" in str(e) or "empty" in msg:
            logger.warning(f"文件内容为空: {e}")
            return error_response(ErrorCode.EMPTY_FILE, str(e))
        else:
            logger.warning(f"参数无效: {e}")
            return error_response(ErrorCode.INVALID_PARAM, str(e))
    elif isinstance(e, TimeoutError):
        logger.error(f"处理超时: {e}")
        return error_response(ErrorCode.TIMEOUT, "处理超时，请稍后重试")
    elif "embedding" in str(e).lower() or "qwen" in str(e).lower() or "dashscope" in str(e).lower():
        logger.error(f"AI 接口调用失败: {e}")
        return error_response(ErrorCode.API_ERROR, f"AI 接口调用失败: {str(e)}")
    elif "chroma" in str(e).lower() or "database" in str(e).lower() or "db" in str(e).lower():
        logger.error(f"数据库错误: {e}")
        return error_response(ErrorCode.DB_ERROR, f"数据库错误: {str(e)}")
    else:
        logger.error(f"未知异常 [{type(e).__name__}]: {e}")
        return error_response(ErrorCode.UNKNOWN, f"{type(e).__name__}: {str(e)}")


# ===================== API 路由 =====================

@app.get("/", tags=["系统"])
async def root():
    """根路径，返回服务信息"""
    return success_response({
        "service": "RAG Document AI API",
        "version": "1.0.0",
        "status": "running"
    })

@app.get("/ping", tags=["系统"])
async def ping():
    """健康检查接口"""
    return success_response({"status": "pong"})

@app.get("/models", tags=["系统"])
async def get_models():
    """获取可用模型列表"""
    models_info = [
        {
            "id": m["id"],
            "name": m["name"],
            "provider": m["provider"],
        }
        for m in AVAILABLE_MODELS
    ]
    return success_response({"models": models_info})

@app.get("/count", tags=["知识库管理"])
async def get_count():
    """获取知识库文档总数"""
    try:
        total = db_manager.count()
        return success_response({"count": total})
    except Exception as e:
        return _handle_exception(e)

@app.post("/query", tags=["RAG 问答"])
def query_rag(request: QueryRequest):
    """
    RAG 智能问答接口
    传入问题，返回答案、参考来源和耗时统计
    """
    try:
        if not request.question or not request.question.strip():
            return error_response(ErrorCode.INVALID_PARAM, "问题不能为空")

        logger.info(f"[RAG问答] 问题: \"{request.question}\" | 模型: {request.model_id or '默认'}")

        result = run_rag_with_sources(request.question, model_id=request.model_id)

        # 检查 LLM 返回是否有错误
        if not result["answer"] and result["meta"].get("tokens", 0) == 0:
            error_msg = result["meta"].get("error", "AI 服务调用失败")
            logger.error(f"[RAG问答] AI 服务调用失败: {error_msg}")
            return error_response(ErrorCode.API_ERROR, f"AI 服务调用失败: {error_msg}")

        meta = result.get("meta", {})
        logger.info(
            f"[RAG问答] 完成 | 总耗时: {meta.get('time', 0)}s | "
            f"检索: {meta.get('retrieve_time', 0)}s | LLM: {meta.get('llm_time', 0)}s | "
            f"Tokens: {meta.get('tokens', 0)} | 模型: {meta.get('model', '?')}"
        )

        return success_response(result)
    except Exception as e:
        return _handle_exception(e)

@app.post("/search", tags=["知识库管理"])
async def search_docs(request: SearchRequest):
    """
    相似度检索接口
    传入查询文本，返回最相关的文档片段
    """
    try:
        if not request.query or not request.query.strip():
            return error_response(ErrorCode.INVALID_PARAM, "查询文本不能为空")
        
        docs_with_scores = db_manager.similarity_search_with_score(
            request.query, k=request.k
        )
        
        results = [
            {
                "content": doc.page_content,
                "metadata": doc.metadata,
                "score": float(score)
            }
            for doc, score in docs_with_scores
        ]
        
        return success_response({"results": results})
    except Exception as e:
        return _handle_exception(e)

@app.post("/add_file", tags=["知识库管理"])
async def add_file(request: AddFileRequest):
    """
    添加本地文件到知识库（增量更新）
    传入文件的本地绝对路径，若该文件已存在则先删除旧文档再重新添加
    """
    try:
        file_path = request.file_path
        if not file_path:
            return error_response(ErrorCode.INVALID_PARAM, "文件路径不能为空")
        
        if not os.path.exists(file_path):
            return error_response(ErrorCode.FILE_NOT_FOUND, f"文件不存在: {file_path}")
        
        logger.info(f"[添加文件] 开始处理: {file_path}")

        # 加载并切分文件
        splits = load_knowledge_file(file_path)
        file_name = os.path.basename(file_path)
        logger.info(f"[添加文件] 文件解析完成: {file_name} | 分块数: {len(splits)}")
        
        # 为每个文档块添加文件来源元数据
        for doc in splits:
            doc.metadata["source_file"] = file_name
            doc.metadata["source_path"] = file_path
        
        # 增量更新：先删除该文件之前的旧文档，避免重复
        deleted_count = db_manager.delete_by_source(file_path)
        if deleted_count > 0:
            logger.info(f"[添加文件] 增量更新: 删除旧文档 {deleted_count} 条")
        
        # 添加新文档到向量库
        ids = db_manager.add_documents(splits)
        logger.info(f"[添加文件] 完成: {file_name} | 新增 {len(splits)} 块 | 当前库总量: {db_manager.count()}")
        
        return success_response({
            "ids": ids,
            "chunk_count": len(splits),
            "file_name": file_name,
            "updated": deleted_count > 0,
            "deleted_count": deleted_count
        })
    except Exception as e:
        return _handle_exception(e)

@app.post("/upload_file", tags=["知识库管理"])
async def upload_file(file: UploadFile = File(...)):
    """
    上传文件并添加到知识库（增量更新）
    支持 multipart/form-data 方式上传文件，若该文件已存在则先删除旧文档再重新添加
    """
    try:
        logger.info(f"[上传文件] 开始上传: {file.filename} | 大小: {file.size} 字节")

        # 保存上传的文件到临时目录
        file_location = os.path.join(UPLOAD_DIR, file.filename)
        with open(file_location, "wb") as f:
            content = await file.read()
            f.write(content)
        
        # 加载并切分文件
        splits = load_knowledge_file(file_location)
        logger.info(f"[上传文件] 文件解析完成: {file.filename} | 分块数: {len(splits)}")
        
        # 为每个文档块添加文件来源元数据
        for doc in splits:
            doc.metadata["source_file"] = file.filename
            doc.metadata["source_path"] = file_location
        
        # 增量更新：先删除该文件之前的旧文档，避免重复
        deleted_count = db_manager.delete_by_source(file_location)
        if deleted_count > 0:
            logger.info(f"[上传文件] 增量更新: 删除旧文档 {deleted_count} 条")
        
        # 添加新文档到向量库
        ids = db_manager.add_documents(splits)
        logger.info(f"[上传文件] 完成: {file.filename} | 新增 {len(splits)} 块 | 当前库总量: {db_manager.count()}")
        
        return success_response({
            "ids": ids,
            "chunk_count": len(splits),
            "file_name": file.filename,
            "updated": deleted_count > 0,
            "deleted_count": deleted_count
        })
    except Exception as e:
        return _handle_exception(e)

@app.post("/add_texts", tags=["知识库管理"])
async def add_texts(request: AddTextsRequest):
    """
    批量添加文本到知识库
    """
    try:
        if not request.texts:
            return error_response(ErrorCode.INVALID_PARAM, "文本列表不能为空")
        
        ids = db_manager.add_texts(request.texts, request.metadatas)
        return success_response({"ids": ids, "count": len(ids)})
    except Exception as e:
        return _handle_exception(e)

@app.post("/delete_by_ids", tags=["知识库管理"])
async def delete_by_ids(request: DeleteByIdsRequest):
    """
    按文档ID删除
    """
    try:
        if not request.ids:
            return error_response(ErrorCode.INVALID_PARAM, "ID列表不能为空")
        
        db_manager.delete_by_ids(request.ids)
        logger.info(f"[删除文档] 按ID删除 {len(request.ids)} 条 | 剩余: {db_manager.count()}")
        return success_response({"status": "success", "deleted_count": len(request.ids)})
    except Exception as e:
        return _handle_exception(e)

@app.post("/delete_by_source", tags=["知识库管理"])
async def delete_by_source(request: DeleteBySourceRequest):
    """
    按源文件路径删除关联的所有文档
    """
    try:
        if not request.source:
            return error_response(ErrorCode.INVALID_PARAM, "源文件路径不能为空")
        
        deleted_count = db_manager.delete_by_source(request.source)
        logger.info(f"[删除文档] 按源文件删除: {request.source} | 删除 {deleted_count} 条 | 剩余: {db_manager.count()}")
        return success_response({"status": "success", "deleted_count": deleted_count})
    except Exception as e:
        return _handle_exception(e)

@app.post("/delete_all", tags=["知识库管理"])
async def delete_all():
    """
    清空知识库所有文档
    """
    try:
        count_before = db_manager.count()
        db_manager.delete_all()
        logger.warning(f"[清空知识库] 已清空全部文档: {count_before} 条")
        return success_response({"status": "success", "deleted_count": count_before})
    except Exception as e:
        return _handle_exception(e)

@app.post("/update_text", tags=["知识库管理"])
async def update_text(request: UpdateTextRequest):
    """
    更新指定ID的文档内容
    """
    try:
        if not request.id:
            return error_response(ErrorCode.INVALID_PARAM, "文档ID不能为空")
        if not request.text:
            return error_response(ErrorCode.INVALID_PARAM, "文本内容不能为空")
        
        db_manager.update_text(request.id, request.text, request.metadata)
        logger.info(f"[更新文档] ID: {request.id} | 内容长度: {len(request.text)}")
        return success_response({"status": "success", "id": request.id})
    except Exception as e:
        return _handle_exception(e)

@app.get("/get_all", tags=["知识库管理"])
async def get_all():
    """
    获取知识库所有文档
    """
    try:
        result = db_manager.get_all()
        return success_response({
            "ids": result["ids"],
            "documents": result["documents"],
            "metadatas": result["metadatas"]
        })
    except Exception as e:
        return _handle_exception(e)

@app.post("/reload_knowledge", tags=["知识库管理"])
async def reload_knowledge():
    """
    重新加载默认知识库文件
    """
    try:
        logger.info("[重载知识库] 开始清空并重新加载默认知识库")
        db_manager.delete_all()
        if os.path.exists(DOC_PATH):
            splits = load_knowledge_file(DOC_PATH)
            db_manager.add_documents(splits)
            logger.info(f"[重载知识库] 完成 | 重新加载 {len(splits)} 块文档")
            return success_response({"status": "success", "chunk_count": len(splits)})
        else:
            logger.warning(f"[重载知识库] 知识库文件不存在: {DOC_PATH}")
            return error_response(ErrorCode.FILE_NOT_FOUND, f"知识库文件不存在: {DOC_PATH}")
    except Exception as e:
        return _handle_exception(e)

# ===================== 服务启动入口 =====================

if __name__ == "__main__":
    import uvicorn
    # 启动 FastAPI 服务，监听本地 8000 端口
    logger.info("=" * 60)
    logger.info("RAG Document AI API 服务启动中...")
    logger.info(f"向量库持久化目录: {CHROMA_DIR}")
    logger.info(f"当前向量库文档数量: {db_manager.count()}")
    logger.info(f"可用模型: {[m['id'] for m in AVAILABLE_MODELS]}")
    logger.info("=" * 60)
    uvicorn.run(
        app, 
        host="127.0.0.1", 
        port=8000,
        timeout_keep_alive=300,
    )
