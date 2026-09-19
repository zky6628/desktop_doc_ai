-- 0009: 查询分段指标、候选与客户端遥测
-- query_runs 后补分段耗时与生成用量列（QueryTrace 契约字段）；
-- retrieval_candidates 保存每个候选的各阶段排名/分数（评测数据主体，
-- 未进入上下文的候选同样保留）；query_client_metrics 以查询为主键
-- 记录客户端 TTFT（服务端按时间戳计算，不接收正文与密钥）

ALTER TABLE query_runs ADD COLUMN retrieval_ms INTEGER;
ALTER TABLE query_runs ADD COLUMN rerank_ms INTEGER;
ALTER TABLE query_runs ADD COLUMN prompt_build_ms INTEGER;
ALTER TABLE query_runs ADD COLUMN model_ttft_ms INTEGER;
ALTER TABLE query_runs ADD COLUMN input_tokens INTEGER;
ALTER TABLE query_runs ADD COLUMN output_tokens INTEGER;

CREATE TABLE retrieval_candidates (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    query_run_id TEXT NOT NULL
        REFERENCES query_runs(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    source TEXT NOT NULL CHECK (source IN ('vector', 'keyword', 'dual')),
    vector_rank INTEGER,
    vector_score REAL,
    keyword_rank INTEGER,
    keyword_score REAL,
    rrf_rank INTEGER NOT NULL,
    rrf_score REAL NOT NULL,
    rerank_rank INTEGER,                  -- 降级时为空（未重排）
    rerank_score REAL,
    in_context INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (query_run_id, chunk_id)
);

CREATE INDEX idx_retrieval_candidates_run
ON retrieval_candidates(query_run_id);

CREATE TABLE query_client_metrics (
    query_run_id TEXT PRIMARY KEY
        REFERENCES query_runs(id) ON DELETE CASCADE,
    client_send_at TEXT NOT NULL,
    first_sse_token_received_at TEXT NOT NULL,
    first_token_rendered_at TEXT NOT NULL,
    client_ttft_ms INTEGER NOT NULL,      -- 服务端按时间戳计算（渲染 - 发送）
    client_instance_id_hash TEXT,         -- 客户端实例随机 UUID 的 SHA-256（可空）
    network_context_json TEXT,
    reported_at TEXT NOT NULL
);
