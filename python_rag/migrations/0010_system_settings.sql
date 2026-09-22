-- 0010: 系统设置表
-- 运行时可变的轻量键值设置；与不可变的 pipeline_configs 版本历史
-- 互补：本表只存"当前选择"，参数事实仍以配置版本行为准

CREATE TABLE system_settings (
    key TEXT PRIMARY KEY,                 -- 设置键（如 chunking_override）
    value_json TEXT NOT NULL,             -- 设置值（JSON 文本）
    updated_at TEXT NOT NULL              -- UTC ISO-8601
);
