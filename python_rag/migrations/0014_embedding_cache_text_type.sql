-- 0014: 嵌入缓存表纳入文本侧别
-- 查询侧与文档侧使用不同 text_type 参数，同文本在两侧产出不同向量；
-- 原主键（模型 + 内容哈希）无法区分侧别，重建表把侧别纳入主键，
-- 既有行全部归属文档侧（表仅承载文档侧写入，数据迁移零损失）
CREATE TABLE embedding_cache_new (
    model_name TEXT NOT NULL,             -- 产生向量的嵌入模型
    text_type TEXT NOT NULL,              -- 产出向量时的文本侧别
    content_hash TEXT NOT NULL,           -- 文本内容 SHA-256
    vector BLOB NOT NULL,                 -- float32 小端序列化向量
    dimensions INTEGER NOT NULL,          -- 向量维度（完整性校验）
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    PRIMARY KEY (model_name, text_type, content_hash)
);

INSERT INTO embedding_cache_new
    (model_name, text_type, content_hash, vector, dimensions, created_at)
SELECT model_name, 'document', content_hash, vector, dimensions, created_at
FROM embedding_cache;

DROP TABLE embedding_cache;
ALTER TABLE embedding_cache_new RENAME TO embedding_cache;
