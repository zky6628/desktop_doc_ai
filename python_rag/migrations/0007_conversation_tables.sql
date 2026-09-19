-- 0007: 会话、消息与引用快照表
-- 引用与助手消息关联（规格合同）：SQLite 无法后补外键，故三表同批次
-- 创建；会话删除时消息级联、消息删除时引用级联，切片清理只置空引用
-- 的切片指针（快照事实保留，历史会话仍可完整展示）

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    knowledge_base_id TEXT NOT NULL
        REFERENCES knowledge_bases(id) ON DELETE RESTRICT,
    title TEXT,                           -- 会话标题（可空，v1 未暴露命名入口）
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    updated_at TEXT NOT NULL
);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,                -- 消息正文（助手消息在生成完成后写入）
    created_at TEXT NOT NULL
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);

-- 引用快照：文件名/版本号/页码/章节/引文与四类分数在创建时定格。
-- 未通过校验的引用同样落库（validation_state=rejected）：编号未知时
-- 文档事实不可知（可空），编号已知但引文不匹配时仍携带文档事实
CREATE TABLE citations (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    assistant_message_id TEXT NOT NULL
        REFERENCES messages(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    citation_order INTEGER NOT NULL,      -- 消息内引用序号（1 起）
    knowledge_base_id_snapshot TEXT NOT NULL,
    document_id_snapshot TEXT,
    document_version_id_snapshot TEXT,
    file_name_snapshot TEXT,
    version_no_snapshot INTEGER,
    quoted_text_snapshot TEXT NOT NULL,
    page_no INTEGER,
    section_path TEXT,
    source_locator_json TEXT,
    validation_state TEXT NOT NULL
        CHECK (validation_state IN ('validated', 'rejected')),
    vector_score REAL,
    keyword_score REAL,
    fusion_score REAL,
    rerank_score REAL,
    created_at TEXT NOT NULL,
    UNIQUE (assistant_message_id, citation_order)
);
