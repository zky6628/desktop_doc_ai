-- 0004: 内容表（解析事实与切片）

-- 解析产物事实：一次解析产出的结构化块序列
CREATE TABLE content_blocks (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    document_version_id TEXT NOT NULL
        REFERENCES document_versions(id),
    parent_block_id TEXT
        REFERENCES content_blocks(id),    -- 层级块结构的父块（可空=顶层块）
    block_type TEXT NOT NULL,             -- heading/paragraph/list/table/image_ocr/header/footer（可扩展）
    ordinal INTEGER NOT NULL,             -- 同版本内的顺序号
    page_no INTEGER,                      -- 页码（可空：无页面概念的内容）
    section_path TEXT,                    -- 章节路径
    content_text TEXT,                    -- 文本内容（表格块可空，结构在 tables 表）
    bbox_json TEXT,                       -- 版面坐标（JSON 文本，可空）
    source_locator_json TEXT,             -- 原始来源定位（JSON 文本，可空）
    content_hash TEXT NOT NULL            -- 块内容哈希（增量与幂等检测）
);

CREATE INDEX idx_content_blocks_version
ON content_blocks(document_version_id);

-- 表格块的扩展证据表：与 content_blocks 一对一（block_id 即主键）。
-- raw_* 与 structure_json 是引用证据；searchable_text 只是检索辅助
CREATE TABLE tables (
    block_id TEXT PRIMARY KEY
        REFERENCES content_blocks(id),
    raw_html TEXT,                        -- 原始 HTML 结构
    raw_markdown TEXT,                    -- 原始 Markdown 结构
    structure_json TEXT,                  -- 结构化单元格数据（JSON 文本）
    searchable_text TEXT,                 -- 序列化后的可检索文本
    serialization_model TEXT,             -- 产出 searchable_text 的模型
    serialization_version TEXT            -- 序列化提示词/流程版本
);

-- 切片产物：属于某个 IndexVersion 的检索单元
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    index_version_id TEXT NOT NULL
        REFERENCES index_versions(id),
    parent_chunk_id TEXT
        REFERENCES chunks(id) ON DELETE SET NULL,
    ordinal INTEGER NOT NULL,             -- 同索引版本内的顺序号
    content TEXT NOT NULL,                -- 切片文本
    token_count INTEGER,                  -- token 数
    section_path TEXT,                    -- 章节路径
    page_start INTEGER,                   -- 起始页码
    page_end INTEGER,                     -- 结束页码
    content_hash TEXT NOT NULL,           -- 切片内容哈希（增量与幂等检测）
    metadata_json TEXT                    -- 附加元数据（JSON 文本，可空）
);

-- 同一索引版本内切片序号唯一
CREATE UNIQUE INDEX uq_chunks_version_ordinal
ON chunks(index_version_id, ordinal);

CREATE INDEX idx_chunks_parent ON chunks(parent_chunk_id);

-- 切片与解析块的定位关系：引用定位链为 chunk -> link -> block -> table/来源定位。
-- chunk 删除时关联随级联消失（关联表无独立生命周期）；
-- block 侧保留默认约束，防止解析事实被悬挂引用
CREATE TABLE chunk_block_links (
    chunk_id TEXT NOT NULL
        REFERENCES chunks(id) ON DELETE CASCADE,
    block_id TEXT NOT NULL
        REFERENCES content_blocks(id),
    relation_type TEXT NOT NULL,          -- 关系类型（如 exact/summary，随引用合同冻结）
    PRIMARY KEY (chunk_id, block_id)
);

CREATE INDEX idx_chunk_block_links_block
ON chunk_block_links(block_id);
