-- 0006: 关键词索引（FTS5）

-- 中文关键词索引虚拟表：写入 jieba 预分词后的空格分隔文本，
-- 由 unicode61 tokenizer 建立倒排（供应方按空格切词）。
-- 全局单表 + fts_namespace 隔离：动态建表会绕过显式迁移管理，
-- 因此命名空间作为行属性而非独立表存在；chunk_id 指回切片主键，
-- 原始文本不入本表（统一以 chunks.content 为准）
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    fts_namespace UNINDEXED,
    chunk_id UNINDEXED,
    content,
    tokenize = 'unicode61'
);

-- 索引版本的关键词索引配置引用：补齐索引构建四类配置的登记位
-- （解析/切片/嵌入已在既有列，分词规则版本经配置行版本化）
ALTER TABLE index_versions ADD COLUMN keyword_config_id TEXT REFERENCES pipeline_configs(id);
