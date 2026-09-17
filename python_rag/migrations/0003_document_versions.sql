-- 0003: 文档版本与索引版本表
-- document_versions.active_index_version_id 的 FK 要求 index_versions
-- 在本迁移内就位，因此 Version/Index 两表同批次创建

CREATE TABLE document_versions (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    document_id TEXT NOT NULL
        REFERENCES documents(id) ON DELETE RESTRICT,
    version_no INTEGER NOT NULL,          -- 文档内递增版本号
    source_path TEXT NOT NULL,            -- 原始文件受控路径；物理清理后转为 tombstone 描述
    source_sha256 TEXT NOT NULL,          -- 源内容哈希（版本创建时的内容事实）
    mime_type TEXT,                       -- MIME 类型
    size_bytes INTEGER,                   -- 源文件字节数
    parser_mode TEXT,                     -- 解析模式：local/mineru（Parser Router 决策结果）
    parser_provider TEXT,                 -- 解析供应方：local/mineru
    parser_version TEXT,                  -- 解析器版本
    parsed_content_sha256 TEXT,           -- 归一化解析产物哈希（未解析时为空）
    status TEXT NOT NULL,                 -- 版本生命周期状态（枚举随激活事务合同冻结）
    -- 活动 IndexVersion 指针：可空 FK。激活事务必须校验其与
    -- active IndexVersion 双向一致
    active_index_version_id TEXT REFERENCES index_versions(id),
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    activated_at TEXT                     -- 激活时间（可空=尚未激活）
);

-- 同一文档内版本号唯一
CREATE UNIQUE INDEX uq_document_versions_doc_version
ON document_versions(document_id, version_no);

-- 索引版本：一次索引构建的不可变事实（staging -> validating -> active -> retired/failed）
CREATE TABLE index_versions (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    document_version_id TEXT NOT NULL
        REFERENCES document_versions(id) ON DELETE RESTRICT,
    index_no INTEGER NOT NULL,            -- 版本内递增索引号
    status TEXT NOT NULL
        CHECK (status IN ('staging', 'validating', 'active', 'retired', 'failed')),
    parser_config_id TEXT,                -- pipeline_configs(config_type='parser') 引用（M4 冻结语义）
    chunking_config_id TEXT,              -- pipeline_configs(config_type='chunking') 引用（M4 冻结语义）
    embedding_profile_id TEXT,            -- Embedding 配置/模型档案引用（M4 冻结语义）
    vector_collection TEXT,               -- Chroma collection 名（staging 期可空）
    fts_namespace TEXT,                   -- FTS5 命名空间（staging 期可空）
    chunk_count INTEGER,                  -- 完整性校验通过后的 Chunk 数
    integrity_hash TEXT,                  -- 索引内容完整性哈希
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    activated_at TEXT,                    -- 激活时间（可空=尚未激活）
    retired_at TEXT                       -- 退役时间（可空=未退役）
);

-- 同一 DocumentVersion 不能有两个 active IndexVersion
CREATE UNIQUE INDEX uq_index_versions_one_active
ON index_versions(document_version_id) WHERE status = 'active';
