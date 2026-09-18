-- 0005: 任务引擎表（tasks / task_events / external_tasks）

-- 任务：生命周期 state 与处理阶段 stage 分离，阶段名不得写入 state。
-- 知识库/文档/版本/索引外键使用 ON DELETE SET NULL：
-- 业务对象未来被清理时，历史任务仍作为审计事实保留
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    task_type TEXT NOT NULL,              -- 任务类型：import/delete/health_check 等（开放枚举，随 API 合同扩展）
    knowledge_base_id TEXT
        REFERENCES knowledge_bases(id) ON DELETE SET NULL,
    document_id TEXT
        REFERENCES documents(id) ON DELETE SET NULL,
    document_version_id TEXT
        REFERENCES document_versions(id) ON DELETE SET NULL,
    index_version_id TEXT
        REFERENCES index_versions(id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'waiting_user', 'running', 'waiting_external', 'retry_waiting',
        'cancel_requested', 'succeeded', 'failed', 'cancelled')),
    stage TEXT CHECK (stage IN (
        'validating', 'storing_file', 'routing_parser', 'parsing_local',
        'submitting_cloud', 'polling_cloud', 'downloading_cloud_result',
        'normalizing', 'chunking', 'embedding',
        'writing_vector_index', 'writing_keyword_index', 'validating_index',
        'activating_version', 'cleaning_up', 'completed')),
    progress REAL NOT NULL DEFAULT 0      -- 处理进度，0..1
        CHECK (progress >= 0 AND progress <= 1),
    priority INTEGER NOT NULL DEFAULT 0,  -- 队列排序优先级（数值越大越先）
    idempotency_key TEXT,                 -- 提交方幂等键：重放必须命中同一任务
    retry_count INTEGER NOT NULL DEFAULT 0,           -- 当前阶段已安排的业务自动重试次数（0..3）
    max_retries INTEGER NOT NULL DEFAULT 3,           -- 业务自动重试上限
    attempt_count INTEGER NOT NULL DEFAULT 0,         -- 任务被领取并开始执行的次数
    stage_attempt INTEGER NOT NULL DEFAULT 0,         -- 当前阶段执行次数（阶段成功后归零）
    total_attempt_count INTEGER NOT NULL DEFAULT 0,   -- 所有阶段累计执行次数（硬上限 12）
    next_retry_at TEXT,                   -- 自动重试到期时间（UTC，可空=未排队重试）
    lease_owner TEXT,                     -- 持有执行租约的 Worker 标识
    lease_expires_at TEXT,                -- 租约到期时间
    heartbeat_at TEXT,                    -- 最近心跳时间
    cancel_requested_at TEXT,             -- 取消请求时间（进入 cancel_requested 时写入）
    checkpoint_json TEXT,                 -- 最近已提交阶段、批次游标、内容哈希与外部引用
    parent_task_id TEXT
        REFERENCES tasks(id) ON DELETE SET NULL,  -- 手动重试派生来源；原任务保持终态不复活
    retry_origin TEXT
        CHECK (retry_origin IS NULL OR retry_origin IN ('manual')),
    error_code TEXT,                      -- 机器可读错误码（脱敏）
    error_message TEXT,                   -- 用户可见错误信息（不含内部路径与凭据）
    input_json TEXT,                      -- 任务输入参数（JSON 文本，不含密钥）
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    started_at TEXT,                      -- 首次进入 running 的时间
    finished_at TEXT                      -- 进入终态的时间
);

-- 容量统计与任务列表均按 state 过滤
CREATE INDEX idx_tasks_state ON tasks(state);

CREATE INDEX idx_tasks_knowledge_base ON tasks(knowledge_base_id);

-- 幂等键唯一：同键重放返回同一任务；部分索引允许多个无键任务并存
CREATE UNIQUE INDEX uq_tasks_idempotency_key
ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- 任务审计事件：创建、状态迁移、迁移被拒等按写入顺序追加；
-- 任务是审计事实，事件外键用 RESTRICT 阻止任务随事件被删除
CREATE TABLE task_events (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    task_id TEXT NOT NULL
        REFERENCES tasks(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,             -- created/state_changed/transition_rejected 等
    state TEXT NOT NULL,                  -- 事件时刻的任务状态（迁移事件为目标状态）
    stage TEXT,                           -- 事件时刻的处理阶段（可空=尚未进入任何阶段）
    attempt_count INTEGER NOT NULL DEFAULT 0,  -- 事件时刻的累计领取次数
    worker TEXT,                          -- 事件时刻的租约持有者（可空）
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    duration_ms INTEGER,                  -- 事件覆盖的耗时（可空）
    checkpoint_json TEXT,                 -- 事件时刻的 checkpoint 快照（可空）
    error_code TEXT,                      -- 失败类事件的错误码（可空）
    detail_json TEXT                      -- 脱敏后的补充详情（JSON 文本，可空）
);

CREATE INDEX idx_task_events_task ON task_events(task_id);

-- 外部任务：云端解析供应方侧的提交与轮询事实。
-- 稳定唯一键为 (provider, provider_batch_ref, source_ref)：
-- 重启恢复以批次与源引用定位，可空的 provider_task_id 只是观测值，
-- 不作为恢复前提，因此不参与唯一约束
CREATE TABLE external_tasks (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    task_id TEXT NOT NULL
        REFERENCES tasks(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,               -- 供应方标识（如 mineru）
    provider_batch_ref TEXT NOT NULL,     -- 供应方批次引用
    source_ref TEXT NOT NULL,             -- 批次内源文件引用
    provider_task_id TEXT,                -- 供应方任务 ID（可空观测值）
    upload_url_expires_at TEXT,           -- 预签名上传地址过期时间
    remote_cancel_state TEXT,             -- 远端取消请求状态
    provider_status_summary TEXT,         -- 供应方状态摘要
    state TEXT NOT NULL,                  -- 供应方侧状态（由 Adapter 按供应方语义写入）
    poll_count INTEGER NOT NULL DEFAULT 0,  -- 已执行轮询次数
    last_polled_at TEXT,                  -- 最近轮询时间
    request_summary_json TEXT,            -- 提交摘要（JSON 文本，不含密钥）
    result_uri TEXT,                      -- 结果下载地址
    result_sha256 TEXT,                   -- 结果内容哈希
    expires_at TEXT                       -- 结果过期时间
);

-- 稳定唯一键：防重复提交同一批次内的同一源文件
CREATE UNIQUE INDEX uq_external_tasks_provider_ref
ON external_tasks(provider, provider_batch_ref, source_ref);

CREATE INDEX idx_external_tasks_task ON external_tasks(task_id);
