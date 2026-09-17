-- 0001: 配置版本表
-- 配置记录不可原地修改；新配置通过新增 version 行表达

-- 解析/切片/Embedding/检索等流水线配置的不可变版本历史
CREATE TABLE pipeline_configs (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    config_type TEXT NOT NULL,            -- 配置类型：parser/chunking/embedding/retrieval/prompt/rerank/generation
    version INTEGER NOT NULL,             -- 类型内的递增版本号
    config_json TEXT NOT NULL,            -- 完整配置内容（JSON 文本）
    config_hash TEXT NOT NULL,            -- config_json 的内容哈希（完整性校验）
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    retired_at TEXT                       -- 退役时间（可空=在役）
);

-- 同一配置类型内版本号唯一：版本链是不可变历史
CREATE UNIQUE INDEX uq_pipeline_configs_type_version
ON pipeline_configs(config_type, version);

-- 模型档案：Embedding/LLM/Rerank 等角色的模型绑定事实
CREATE TABLE model_profiles (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    role TEXT NOT NULL,                   -- 模型角色：embedding/generation/rerank
    provider TEXT NOT NULL,               -- 供应方：dashscope/ollama 等
    model_name TEXT NOT NULL,             -- 模型名（如 text-embedding-v4、qwen-plus）
    endpoint_fingerprint TEXT,            -- 端点指纹（脱敏，不存完整 URL 与密钥）
    model_version TEXT,                   -- 供应方模型版本观测值
    parameters_json TEXT,                 -- 模型参数（JSON 文本，可空=使用默认）
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    retired_at TEXT                       -- 退役时间（可空=在役）
);
