# Document AI - RAG 文档智能助手

基于 Flutter 桌面端 + Python FastAPI 后端的文档智能问答系统。

## 项目架构

```
desktop_doc_ai/
├── flutter_app/          # Flutter 桌面端应用
│   ├── lib/
│   │   ├── main.dart          # 主入口和 UI 页面
│   │   ├── models/
│   │   │   └── drop_file_model.dart  # 状态管理模型
│   │   └── services/
│   │       └── rag_service.dart      # HTTP API 调用服务
│   └── pubspec.yaml
│
└── python_rag/           # Python RAG 后端服务
    ├── main.py                # FastAPI 服务主入口
    ├── requirements.txt       # Python 依赖
    ├── .env                   # 环境变量（API Key 等）
    ├── chroma_db/             # ChromaDB 向量数据库
    ├── docs/                  # 默认知识库文件
    └── uploads/               # 上传文件临时目录
```

## 技术栈

### 前端（Flutter）
- Flutter 桌面端（Windows）
- Provider 状态管理
- desktop_drop 文件拖拽
- http 网络请求

### 后端（Python）
- FastAPI Web 框架
- Uvicorn ASGI 服务器
- LangChain RAG 框架
- ChromaDB 向量数据库
- 阿里云 DashScope (Qwen) LLM + Embedding

## 快速开始

### 1. 启动 Python 后端服务

```bash
cd python_rag

# 安装依赖（首次运行）
pip install -r requirements.txt

# 配置 API Key
# 复制 .env.example 为 .env，填入你的 DashScope API Key
copy .env.example .env
# 编辑 .env 文件，设置 DASHSCOPE_API_KEY

# 启动服务
python main.py
```

或双击 ` start_backend.bat` 文件启动

服务启动后访问：http://127.0.0.1:8000/docs 可查看 API 文档

### 2. 启动 Flutter 桌面端

```bash
cd flutter_app

# 安装依赖（首次运行）
flutter pub get

# 运行应用
flutter run -d windows
```

或双击 `start_flutter.bat` 文件启动

启动后应用会自动尝试连接 `http://127.0.0.1:8000` 的后端服务。
如需修改服务地址，点击右上角设置按钮即可。

## 使用说明

1. 确保 Python 后端服务已启动
2. 打开 Flutter 桌面应用，确认右上角指示灯为绿色（已连接）
3. 将文档文件拖拽到左侧文件区域
4. 点击"添加到知识库"按钮，将文件内容导入向量数据库
5. 在右侧问答区域输入问题，点击"提问"获取答案

## API 接口列表

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/ping` | 健康检查 |
| GET | `/count` | 获取知识库文档总数 |
| POST | `/query` | RAG 智能问答 |
| POST | `/search` | 相似度检索 |
| POST | `/add_file` | 添加本地文件（传路径） |
| POST | `/upload_file` | 上传文件（multipart） |
| POST | `/add_texts` | 批量添加文本 |
| POST | `/delete_by_ids` | 按 ID 删除 |
| POST | `/delete_all` | 清空知识库 |
| POST | `/update_text` | 更新文档 |
| GET | `/get_all` | 获取所有文档 |
| POST | `/reload_knowledge` | 重新加载默认知识库 |
