-- 0008: 查询运行与查询事件表
-- 查询为实时路径：QueryRun 是评测与状态的事实源，query_events 承载
-- SSE 事件持久化（stage/citation/terminal 长期保留，token 批次终态后
-- 30 分钟过期）。引用快照回补两列：content_snapshot（05 合同要求的
-- 正文快照，0007 漏列）与 query_run_id（评测关联）

CREATE TABLE query_runs (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    knowledge_base_id TEXT NOT NULL
        REFERENCES knowledge_bases(id) ON DELETE RESTRICT,
    conversation_id TEXT
        REFERENCES conversations(id) ON DELETE SET NULL,
    user_message_id TEXT
        REFERENCES messages(id) ON DELETE SET NULL,
    assistant_message_id TEXT
        REFERENCES messages(id) ON DELETE SET NULL,
    question TEXT NOT NULL,               -- 用户问题（评测事实，规格要求保存）
    state TEXT NOT NULL
        CHECK (state IN ('queued', 'running', 'cancel_requested',
                         'completed', 'failed', 'cancelled')),
    refused INTEGER NOT NULL DEFAULT 0,   -- 无证据拒答（未调用模型）
    rerank_degraded INTEGER NOT NULL DEFAULT 0,  -- 重排降级（使用 RRF 前 5）
    retrieval_config_id TEXT REFERENCES pipeline_configs(id),
    rerank_config_id TEXT REFERENCES pipeline_configs(id),
    context_config_id TEXT REFERENCES pipeline_configs(id),
    generation_config_id TEXT REFERENCES pipeline_configs(id),
    started_at TEXT,
    first_token_at TEXT,
    completed_at TEXT,
    server_ttft_ms INTEGER,               -- 服务端 TTFT（started_at → 首 token）
    total_ms INTEGER,                     -- 创建 → 终态
    error_code TEXT,
    error_message TEXT,
    idempotency_key TEXT,                 -- 创建幂等键（重放返回同一查询）
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_query_runs_idempotency_key
ON query_runs(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX idx_query_runs_kb ON query_runs(knowledge_base_id, created_at);

-- 查询事件：event_seq 查询内单调递增（1 起），SSE 断点重放的事实源；
-- token 事件按批次合并写库（禁止逐 token 落库），expires_at 仅 token
-- 批次携带（终态后 30 分钟过期，过期后清理链回收）
CREATE TABLE query_events (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    query_run_id TEXT NOT NULL
        REFERENCES query_runs(id) ON DELETE CASCADE,
    event_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('meta', 'stage', 'tokens', 'citation',
                              'done', 'error', 'cancelled')),
    payload_json TEXT,                    -- 事件载荷（结构化事实，不含密钥）
    token_text TEXT,                      -- token 批次拼接文本
    token_seq_start INTEGER,
    token_seq_end INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    UNIQUE (query_run_id, event_seq)
);

ALTER TABLE citations ADD COLUMN content_snapshot TEXT;
ALTER TABLE citations ADD COLUMN query_run_id TEXT REFERENCES query_runs(id);
