# Document AI — RAG 文档智能工作台

## 1. 项目简介

此项目是本机文档问答工作台：Flutter Windows 客户端 + 同机 FastAPI 服务，
把本地文档变成可追溯的引用问答能力。链路覆盖解析、切片、向量与关键词双索引、
混合检索、重排、流式生成与引用校验；SQLite 为业务事实源，Chroma 与 FTS5 为可重建派生索引；
导入与索引以任务化流水线执行，支持中断、取消与重试。文档与密钥不出
本机，仅解析与模型推理按需联网，云端解析前需人为确认。

## 2. 项目架构

```text
desktop_doc_ai/
├── flutter_app/                 # Flutter Windows 桌面客户端
│   ├── lib/app/                 # 路由、外壳、服务地址与本地偏好
│   ├── lib/pages/               # 问答 / 知识库 / 任务中心 / 评测 / 设置
│   ├── lib/controllers/         # 各页面状态控制器
│   ├── lib/api/                 # HTTP 客户端、DTO 与 SSE 流式会话
│   ├── lib/widgets/             # 侧边栏、上传与确认对话框、消息气泡
│   └── test/                    # 93 项测试
├── python_rag/                  # 本机 FastAPI 服务
│   ├── main.py                  # 应用装配：迁移、依赖注入、Worker、路由
│   ├── app/domain/              # 领域层：实体、端口、状态机、切片、融合、校验
│   ├── app/infrastructure/      # 适配器层：SQLite、解析、索引、检索、模型网关、Worker
│   ├── app/api/v1/              # 接口层：路由、统一信封、游标分页
│   ├── migrations/              # 顺序 SQL 迁移（0001–0014）
│   ├── tests/                   # 656 项测试
│   ├── data/                    # 事实库、向量目录、评测产物（本地，不入库）
│   └── *.py                     # 验收 / 评测 / 导出 / 指标脚本
├── start_backend.bat            # 一键启动后端
├── start_flutter.bat            # 一键启动桌面端
└── README.md
```

| 模块                              | 职责                                                                    |
| ------------------------------- | --------------------------------------------------------------------- |
| `flutter_app`                   | 桌面交互：知识库与文档管理、任务中心、流式问答与引用定位、评测与设置                                    |
| `python_rag/app/domain`         | 纯领域逻辑：切片与融合规则、任务状态机、引文校验、端口契约（不依赖框架与供应商）                              |
| `python_rag/app/infrastructure` | 端口实现与供应商隔离：SQLite 事实源、解析器、Chroma 向量索引、FTS5 关键词索引、嵌入/重排/生成网关、任务 Worker |
| `python_rag/app/api/v1`         | HTTP 接口：统一信封、稳定错误码、游标分页、SSE 事件流                                       |
| `migrations`                    | 顺序 SQL 迁移：单事务应用、校验和验证                                                 |

## 3. 运行环境

### 3.1 基础环境

| 项              | 要求                                                 |
| -------------- | -------------------------------------------------- |
| 操作系统           | Windows 10 / 11 x64（桌面端仅 Windows；后端为跨平台 Python 服务） |
| Python         | 3.12+（实测 3.12.10），依赖锁定用 `uv`                       |
| Flutter / Dart | Flutter 3.44.2 / Dart 3.12.2                       |
| 端口             | 后端默认 `127.0.0.1:8000`，可在客户端设置页改指向                  |
| 网络             | 导入图片 / 扫描件、以及使用云端模型时需要出网（生成可切本地模型）                 |

### 3.2 外部服务与凭据

| 服务            | 用途                     | 配置                                                            | 凭据                  |
| ------------- | ---------------------- | ------------------------------------------------------------- | ------------------- |
| 阿里云 DashScope | 文本嵌入                   | qwen3.7-text-embedding（1024 维）                                | `DASHSCOPE_API_KEY` |
| 阿里云 DashScope | 重排                     | qwen3-rerank，top\_n=5（模型名可配）                                  | 同上                  |
| 阿里云 DashScope | 流式生成                   | qwen3.8-max（OpenAI 兼容端点，关闭思考模式；模型与供应商可配）                      | 同上                  |
| OpenAI 兼容端点   | 生成（可选）                 | 设 `WORKBENCH_GENERATION_PROVIDER=openai_compatible` 可指向本地推理服务 | 本地端点不校验             |
| MinerU        | 图片 / 扫描件 / 复杂 PDF 云端解析 | 提交前需用户确认                                                      | `MINERU_API_TOKEN`  |

### 3.3 环境变量

复制 `python_rag/.env.example` 为 `python_rag/.env` 后按需填写：

| 变量                              | 默认                                   | 说明                                      |
| ------------------------------- | ------------------------------------ | --------------------------------------- |
| `DASHSCOPE_API_KEY`             | 空                                    | 嵌入 / 重排 / 生成凭据；缺失时相应环节按未配置运行            |
| `MINERU_API_TOKEN`              | 空                                    | 云端解析凭据；缺失时云端路线按认证失败处理                   |
| `WORKBENCH_DB_PATH`             | `python_rag/data/workbench.db`       | 业务事实库（SQLite）路径                         |
| `WORKBENCH_STAGING_DIR`         | `python_rag/uploads/staging`         | 上传暂存受控目录                                |
| `WORKBENCH_CHROMA_DIR`          | `python_rag/data/chroma_workbench`   | 派生向量索引目录（可重建）                           |
| `WORKBENCH_WORKER_ENABLED`      | 启用                                   | 设 `0/false/off` 后任务只入队不执行               |
| `WORKBENCH_LOCAL_DEBUG`         | `0`                                  | 开启后暴露调试检索与客户端评测入口                       |
| `WORKBENCH_JIEBA_USER_DICT`     | `python_rag/data/jieba_userdict.txt` | 领域词典；文件缺失即用默认词典                         |
| `WORKBENCH_RERANK_MODEL`        | `qwen3-rerank`                       | 重排模型名                                   |
| `WORKBENCH_GENERATION_PROVIDER` | `dashscope`                          | 生成供应商：`dashscope` / `openai_compatible` |
| `WORKBENCH_GENERATION_MODEL`    | `qwen3.8-max`                        | 生成模型名                                   |
| `WORKBENCH_GENERATION_BASE_URL` | 随供应商                                 | 生成端点地址                                  |
| `WORKBENCH_GENERATION_API_KEY`  | 随供应商                                 | 生成专用密钥                                  |

模型相关变量改后需重启后端生效；实际生效身份可从 `GET /api/v1/config/public` 核对。

### 3.4 数据与目录

| 资产    | 位置                                  | 说明               |
| ----- | ----------------------------------- | ---------------- |
| 业务事实库 | `python_rag/data/workbench.db`      | 唯一事实源（含 WAL/SHM） |
| 向量索引  | `python_rag/data/chroma_workbench/` | 派生数据，可按索引版本重建    |
| 上传暂存  | `python_rag/uploads/staging/`       | 过期清理             |
| 评测产物  | `python_rag/data/eval-*/`           | 题集、标注、导出与报告      |

## 4. 使用说明

### 4.1 启动后端

```bash
cd python_rag
copy .env.example .env                          # 首次：填入密钥
uv pip install --system -r requirements.lock    # 首次：安装锁定依赖
python main.py                                  # 服务地址 http://127.0.0.1:8000
```

也可双击根目录 `start_backend.bat`：校验 Python 3.12+ 与 uv、缺失时从 `.env.example` 生成 `.env`、
按 `requirements.lock` 安装依赖并启动服务。
接口文档见 `http://127.0.0.1:8000/docs`；存活探针 `GET /ping`。

### 4.2 启动桌面端

```bash
cd flutter_app
flutter pub get                # 首次
flutter run -d windows
```

也可双击 `start_flutter.bat`。应用启动后自动连接 `127.0.0.1:8000`，顶栏指示灯显示连接状态；
改地址在「设置 → 服务连接」。

### 4.3 典型流程

1. 启动后端与桌面端（指示灯为绿）
2. 知识库页新建知识库并上传文档（拖拽或选择，支持 txt / md / docx / pdf 与图片）
3. 任务中心查看导入与索引进度；图片与扫描件需先批准云端解析
4. 问答页选择知识库提问，流式回答中点击 `[S编号]` 定位引用原文
5. 需要量化效果时，用评测页或 `POST /api/v1/evaluation-runs` 跑参数组对比

### 4.4 常用命令

```bash
# 后端
cd python_rag
python -m pytest tests/                 # 656 项
ruff check app/ tests/ main.py          # 静态检查
mypy                                    # 类型检查
python main.py                          # 启动服务

# 前端
cd flutter_app
flutter analyze
flutter test                            # 93 项
```

## 5. API 接口列表

### 5.1 通用约定

- 业务接口统一在 `/api/v1` 下；响应信封为
  `{"success": true, "request_id": "...", "data": {...}, "error": null}`，
  失败时 `error` 为 `{"code", "message", "retryable"}`，`request_id` 用于关联服务端日志。
- 错误码为稳定字符串（如 `INVALID_PARAM`、`TASK_QUEUE_FULL`、`TASK_STATE_CONFLICT`、
  `KB_NOT_FOUND`、`CONFIRMATION_CONFLICT` 等）。
- 长耗时操作返回 `202` 并附任务 ID；列表接口使用游标分页。
- 容量与限制：单批上传 ≤ 50 文件、单文件 ≤ 100 MB；任务排队 ≤ 50、非终态 ≤ 53、并发执行 ≤ 3。
- 可上传格式：`txt` / `md` / `markdown` / `docx` / `pdf` 与图片 `jpg` / `jpeg` / `png` / `bmp` / `webp` / `tiff`。

### 5.2 系统与配置

| 方法  | 路径                 | 说明                                                 |
| --- | ------------------ | -------------------------------------------------- |
| GET | `/health`          | 聚合健康探针：SQLite / Chroma / FTS / Worker 状态、队列水位、凭据状态 |
| GET | `/config/public`   | 公共运行配置：模型身份、在役配置摘要、容量与上传限制、功能开关                    |
| GET | `/config/chunking` | 读取在役切片参数                                           |
| PUT | `/config/chunking` | 写入切片参数（非法值 422）                                    |

### 5.3 知识库与文档

| 方法     | 路径                                   | 说明                                                                                   |
| ------ | ------------------------------------ | ------------------------------------------------------------------------------------ |
| POST   | `/knowledge-bases`                   | 新建知识库                                                                                |
| GET    | `/knowledge-bases`                   | 知识库列表（游标分页）                                                                          |
| GET    | `/knowledge-bases/{kb_id}`           | 知识库详情                                                                                |
| PATCH  | `/knowledge-bases/{kb_id}`           | 重命名                                                                                  |
| DELETE | `/knowledge-bases/{kb_id}`           | 删除知识库                                                                                |
| POST   | `/health/knowledge-bases/{kb_id}`    | 发起索引健康检查任务                                                                           |
| POST   | `/knowledge-bases/{kb_id}/documents` | multipart 批量上传（`parser_preference`、`duplicate_policy`、`Idempotency-Key`），逐文件结果，`202` |
| GET    | `/knowledge-bases/{kb_id}/documents` | 文档列表（游标分页）                                                                           |
| GET    | `/documents/{doc_id}`                | 文档详情                                                                                 |
| GET    | `/documents/{doc_id}/versions`       | 版本历史                                                                                 |
| GET    | `/documents/{doc_id}/blocks`         | 解析块预览（游标分页）                                                                          |
| POST   | `/documents/{doc_id}/replace`        | 替换内容（新版本，`202`）                                                                      |
| POST   | `/documents/{doc_id}/rebuild`        | 从解析事实重建索引（`202`）                                                                     |
| DELETE | `/documents/{doc_id}`                | 删除文档（`202`）                                                                          |

### 5.4 任务

| 方法   | 路径                                    | 说明                                |
| ---- | ------------------------------------- | --------------------------------- |
| GET  | `/tasks`                              | 任务列表（状态 / 知识库 / 文档筛选，游标分页）        |
| GET  | `/tasks/{task_id}`                    | 任务详情（状态、阶段、进度、尝试次数、错误）            |
| GET  | `/tasks/{task_id}/events`             | 任务事件时间线（游标分页）                     |
| POST | `/tasks/{task_id}/cancel`             | 取消任务（幂等）                          |
| POST | `/tasks/{task_id}/retry`              | 重试失败任务（派生新任务）                     |
| POST | `/tasks/{task_id}/cloud-confirmation` | 云端解析确认：`approve` 继续 / `reject` 取消 |

### 5.5 查询（问答）

| 方法   | 路径                                   | 说明                                                |
| ---- | ------------------------------------ | ------------------------------------------------- |
| POST | `/queries`                           | 创建查询，返回 `202` + `query_id` + `stream_url`；支持幂等键重放 |
| GET  | `/queries/{query_id}`                | 聚合结果：答案、引用快照、状态、分段指标                              |
| GET  | `/queries/{query_id}/events`         | SSE 事件流（支持 `Last-Event-ID` 续传）                    |
| POST | `/queries/{query_id}/cancel`         | 取消查询（保留已生成正文与引用）                                  |
| POST | `/queries/{query_id}/client-metrics` | 上报客户端三时间戳遥测，`204`                                 |

SSE 事件：`meta`（查询与配置身份）、`stage`（阶段与降级标注）、`tokens`（增量文本批次）、
`citation`（引用快照：文件、版本、页码、章节、内容与来源定位）、`done`、`error` / `cancelled`。

### 5.6 会话

| 方法     | 路径                                          | 说明              |
| ------ | ------------------------------------------- | --------------- |
| GET    | `/conversations`                            | 会话列表（游标分页）      |
| GET    | `/conversations/{conversation_id}/messages` | 历史消息（助手消息带引用快照） |
| PATCH  | `/conversations/{conversation_id}`          | 重命名会话           |
| DELETE | `/conversations/{conversation_id}`          | 删除会话（级联消息与引用）   |
| DELETE | `/conversations`                            | 清空全部会话          |

### 5.7 指标、调试与评测

| 方法     | 路径                                 | 说明                                          |
| ------ | ---------------------------------- | ------------------------------------------- |
| GET    | `/metrics/queries`                 | 查询指标：状态计数、失败率、TTFT 分位、token 合计、降级计数         |
| DELETE | `/metrics/queries`                 | 重置运行概览：仅删失败与取消的运行                           |
| POST   | `/search`                          | 调试检索：返回各阶段候选与分数；需开启 `WORKBENCH_LOCAL_DEBUG` |
| POST   | `/evaluation-runs`                 | 创建评测运行（参数组 × 问题集）                           |
| GET    | `/evaluation-runs`                 | 评测运行列表                                      |
| GET    | `/evaluation-runs/{run_id}`        | 评测运行详情与分组对比                                 |
| POST   | `/evaluation-runs/{run_id}/cancel` | 取消评测运行（终态恢复原参数）                             |

### 5.8 存活探针

| 方法  | 路径      | 说明                         |
| --- | ------- | -------------------------- |
| GET | `/ping` | 存活探针（客户端连接指示灯据 `200` 判定在线） |

除 `/ping` 外，服务不暴露其他非 `/api/v1` 路径。
