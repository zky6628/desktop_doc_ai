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

# 导入 FastAPI 相关模块
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

# 导入 dotenv 模块，用于从 .env 文件加载环境变量
from dotenv import load_dotenv

# ===================== 编码设置：强制使用 UTF-8 =====================
# 解决 Windows 下控制台默认 GBK 编码导致的中文乱码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')

# 导入 DashScope（阿里云 Qwen 服务）SDK，用于调用 Qwen 的 Embedding 和 LLM 接口
import dashscope
# 从 DashScope 导入文本嵌入类
from dashscope import TextEmbedding

# 导入 LangChain 的文本分割器，用于将长文本切分为小块
from langchain_text_splitters import RecursiveCharacterTextSplitter
# 导入 LangChain 的文档加载器，用于加载文本文件
from langchain_community.document_loaders import TextLoader
# 导入 LangChain 的 Chroma 向量数据库封装
from langchain_chroma import Chroma
# 导入 LangChain 的 Embeddings 基类，用于自定义 Embedding
from langchain_core.embeddings import Embeddings
# 导入 LangChain 的 Qwen Chat 模型封装（通过 OpenAI 兼容接口）
from langchain_openai import ChatOpenAI
# 导入 LangChain 的提示词模板，用于构建对话提示
from langchain_core.prompts import ChatPromptTemplate
# 导入 LangChain 的可运行组件，用于构建 RAG 管道
from langchain_core.runnables import RunnablePassthrough
# 导入 LangChain 的输出解析器，用于解析 LLM 输出
from langchain_core.output_parsers import StrOutputParser

# ===================== 环境变量与路径配置 =====================

# 获取当前脚本所在目录的绝对路径，确保路径不随运行目录变化
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
# LLM 模型名称，使用 Qwen 的对话模型
LLM_MODEL = "qwen-turbo"
# Qwen 的 OpenAI 兼容接口地址
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

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
    加载并切分知识库文本文件
    :param file_path: 知识库文件路径
    :return: 切分后的 Document 对象列表
    """
    loader = TextLoader(file_path, encoding="utf-8")
    docs = loader.load()
    
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=300,
        chunk_overlap=50,
        separators=["\n\n", "\n", "。", "，", " ", ""]
    )
    
    splits = splitter.split_documents(docs)
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

# ===================== RAG 问答链构建 =====================

def build_rag_chain(retriever):
    """
    构建 RAG 问答链（使用 LCEL 语法）
    """
    prompt = ChatPromptTemplate.from_template("""
你是一个专业的文档助手，请根据下面的上下文回答用户的问题。
要求：
1. 只使用上下文信息回答，不要编造内容
2. 如果上下文中没有答案，请明确回答"根据现有知识库无法回答该问题"
3. 回答要简洁、准确、有条理

<context>
{context}
</context>

用户问题：{input}
""")

    rag_chain = (
        {
            "context": retriever,
            "input": RunnablePassthrough()
        }
        | prompt
        | llm
        | StrOutputParser()
    )
    
    return rag_chain

# ===================== 全局实例初始化（服务启动时执行一次） =====================

# 初始化自定义的 Qwen Embedding 实例
embeddings = QwenEmbeddings(model_name=EMBEDDING_MODEL)

# 初始化 Qwen 聊天模型实例（通过 OpenAI 兼容接口调用）
llm = ChatOpenAI(
    model=LLM_MODEL,
    api_key=DASHSCOPE_API_KEY,
    base_url=QWEN_BASE_URL,
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

# 构建 RAG 问答链
rag_chain = build_rag_chain(retriever)

# ===================== RAG 问答封装 =====================

def run_rag_with_sources(question: str):
    """
    执行 RAG 问答，同时返回答案和检索到的源文档
    """
    source_docs = retriever.invoke(question)
    answer = rag_chain.invoke(question)
    return answer, source_docs

# ===================== FastAPI 请求/响应模型 =====================

class QueryRequest(BaseModel):
    """RAG 问答请求模型"""
    question: str = Field(..., description="用户的问题")

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

# ===================== API 路由 =====================

@app.get("/", tags=["系统"])
async def root():
    """根路径，返回服务信息"""
    return {
        "service": "RAG Document AI API",
        "version": "1.0.0",
        "status": "running"
    }

@app.get("/ping", tags=["系统"])
async def ping():
    """健康检查接口"""
    return {"status": "pong"}

@app.get("/count", tags=["知识库管理"])
async def get_count():
    """获取知识库文档总数"""
    try:
        total = db_manager.count()
        return {"count": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/query", tags=["RAG 问答"])
async def query_rag(request: QueryRequest):
    """
    RAG 智能问答接口
    传入问题，返回答案和参考来源
    """
    try:
        if not request.question.strip():
            raise HTTPException(status_code=400, detail="问题不能为空")
        
        answer, source_docs = run_rag_with_sources(request.question)
        
        return {
            "answer": answer,
            "sources": [doc.page_content for doc in source_docs]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

@app.post("/search", tags=["知识库管理"])
async def search_docs(request: SearchRequest):
    """
    相似度检索接口
    传入查询文本，返回最相关的文档片段
    """
    try:
        if not request.query.strip():
            raise HTTPException(status_code=400, detail="查询文本不能为空")
        
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
        
        return {"results": results}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

@app.post("/add_file", tags=["知识库管理"])
async def add_file(request: AddFileRequest):
    """
    添加本地文件到知识库
    传入文件的本地绝对路径
    """
    try:
        file_path = request.file_path
        if not file_path:
            raise HTTPException(status_code=400, detail="文件路径不能为空")
        
        if not os.path.exists(file_path):
            raise HTTPException(status_code=400, detail=f"文件不存在: {file_path}")
        
        # 加载并切分文件
        splits = load_knowledge_file(file_path)
        file_name = os.path.basename(file_path)
        
        # 为每个文档块添加文件来源元数据
        for doc in splits:
            doc.metadata["source_file"] = file_name
            doc.metadata["source_path"] = file_path
        
        # 添加到向量库
        ids = db_manager.add_documents(splits)
        
        return {
            "ids": ids,
            "chunk_count": len(splits),
            "file_name": file_name
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

@app.post("/upload_file", tags=["知识库管理"])
async def upload_file(file: UploadFile = File(...)):
    """
    上传文件并添加到知识库
    支持 multipart/form-data 方式上传文件
    """
    try:
        # 保存上传的文件到临时目录
        file_location = os.path.join(UPLOAD_DIR, file.filename)
        with open(file_location, "wb") as f:
            content = await file.read()
            f.write(content)
        
        # 加载并切分文件
        splits = load_knowledge_file(file_location)
        
        # 为每个文档块添加文件来源元数据
        for doc in splits:
            doc.metadata["source_file"] = file.filename
            doc.metadata["source_path"] = file_location
        
        # 添加到向量库
        ids = db_manager.add_documents(splits)
        
        return {
            "ids": ids,
            "chunk_count": len(splits),
            "file_name": file.filename
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

@app.post("/add_texts", tags=["知识库管理"])
async def add_texts(request: AddTextsRequest):
    """
    批量添加文本到知识库
    """
    try:
        if not request.texts:
            raise HTTPException(status_code=400, detail="文本列表不能为空")
        
        ids = db_manager.add_texts(request.texts, request.metadatas)
        return {"ids": ids, "count": len(ids)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

@app.post("/delete_by_ids", tags=["知识库管理"])
async def delete_by_ids(request: DeleteByIdsRequest):
    """
    按文档ID删除
    """
    try:
        if not request.ids:
            raise HTTPException(status_code=400, detail="ID列表不能为空")
        
        db_manager.delete_by_ids(request.ids)
        return {"status": "success", "deleted_count": len(request.ids)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

@app.post("/delete_all", tags=["知识库管理"])
async def delete_all():
    """
    清空知识库所有文档
    """
    try:
        count_before = db_manager.count()
        db_manager.delete_all()
        return {"status": "success", "deleted_count": count_before}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

@app.post("/update_text", tags=["知识库管理"])
async def update_text(request: UpdateTextRequest):
    """
    更新指定ID的文档内容
    """
    try:
        if not request.id:
            raise HTTPException(status_code=400, detail="文档ID不能为空")
        if not request.text:
            raise HTTPException(status_code=400, detail="文本内容不能为空")
        
        db_manager.update_text(request.id, request.text, request.metadata)
        return {"status": "success", "id": request.id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

@app.get("/get_all", tags=["知识库管理"])
async def get_all():
    """
    获取知识库所有文档
    """
    try:
        result = db_manager.get_all()
        return {
            "ids": result["ids"],
            "documents": result["documents"],
            "metadatas": result["metadatas"]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

@app.post("/reload_knowledge", tags=["知识库管理"])
async def reload_knowledge():
    """
    重新加载默认知识库文件
    """
    try:
        db_manager.delete_all()
        if os.path.exists(DOC_PATH):
            splits = load_knowledge_file(DOC_PATH)
            db_manager.add_documents(splits)
            return {"status": "success", "chunk_count": len(splits)}
        else:
            raise HTTPException(status_code=400, detail=f"知识库文件不存在: {DOC_PATH}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")

# ===================== 服务启动入口 =====================

if __name__ == "__main__":
    import uvicorn
    # 启动 FastAPI 服务，监听本地 8000 端口
    print("正在启动 RAG API 服务...", file=sys.stderr)
    print(f"当前向量库文档数量: {db_manager.count()}", file=sys.stderr)
    uvicorn.run(app, host="127.0.0.1", port=8000)
