-- 0011: 嵌入缓存表
-- 以模型 + 切片内容哈希为键的向量缓存：同内容重导入、参数实验后
-- 恢复重建等场景直接复用既有向量，不重复调用嵌入 API（费用与时延
-- 均免）。换模型后旧向量不跨用，模型名参与主键。

CREATE TABLE embedding_cache (
    model_name TEXT NOT NULL,             -- 产生向量的嵌入模型
    content_hash TEXT NOT NULL,           -- 切片内容哈希（与 chunks.content_hash 同源）
    vector BLOB NOT NULL,                 -- float32 小端序列化向量
    dimensions INTEGER NOT NULL,          -- 向量维度（完整性校验）
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    PRIMARY KEY (model_name, content_hash)
);
