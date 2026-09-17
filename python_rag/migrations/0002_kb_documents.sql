-- 0002: 知识库与文档表

-- 知识库：软删除体系根对象
CREATE TABLE knowledge_bases (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    name TEXT NOT NULL,                   -- 名称；活动记录名称部分唯一（见下方索引）
    description TEXT,                     -- 可空描述
    status TEXT NOT NULL CHECK (status IN ('active', 'deleting', 'deleted', 'failed')),
    deleted_at TEXT,                      -- 软删除时间（可空=活动）
    delete_requested_at TEXT,             -- 删除请求时间（队列满时请求失败不写入）
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    updated_at TEXT NOT NULL              -- UTC ISO-8601
);

-- 活动知识库名称唯一：软删除后允许同名重建
CREATE UNIQUE INDEX uq_knowledge_bases_active_name
ON knowledge_bases(name) WHERE deleted_at IS NULL;

-- 文档：知识库内的内容单元，软删除体系
CREATE TABLE documents (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    knowledge_base_id TEXT NOT NULL
        REFERENCES knowledge_bases(id) ON DELETE RESTRICT,
    display_name TEXT NOT NULL,           -- 用户可见的原文件名
    source_sha256 TEXT NOT NULL,          -- 源内容哈希（活动内容去重键之一）
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'processing', 'ready', 'updating', 'deleting', 'failed', 'deleted')),
    -- 活动版本指针：可空 FK。父表 document_versions 在 0003 创建
    -- （Version/Index 阶段），迁移期间无 DML，应用完成后约束生效
    active_document_version_id TEXT REFERENCES document_versions(id),
    deleted_at TEXT,                      -- 软删除时间（可空=活动）
    delete_requested_at TEXT,             -- 删除请求时间
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    updated_at TEXT NOT NULL              -- UTC ISO-8601
);

-- 活动内容去重：同一知识库内活动文档的源内容唯一；
-- 软删除后允许相同内容重新导入
CREATE UNIQUE INDEX uq_documents_active_source
ON documents(knowledge_base_id, source_sha256) WHERE deleted_at IS NULL;
