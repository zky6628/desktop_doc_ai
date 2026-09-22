-- 0012: 评测运行表
-- 切片参数对比评测的运行事实：问题集与参数组内嵌于运行记录（一次
-- 运行自包含），各组结果以 JSON 快照落库；查询指标事实仍在 query_runs
-- （结果 JSON 携带 query_run_id 关联，查询表不加评测归属列）

CREATE TABLE evaluation_runs (
    id TEXT PRIMARY KEY,                  -- UUIDv7 文本
    knowledge_base_id TEXT NOT NULL
        REFERENCES knowledge_bases(id),
    task_id TEXT NOT NULL UNIQUE
        REFERENCES tasks(id),             -- 编排任务（取消/进度经任务表达）
    target_version_ids_json TEXT NOT NULL,  -- 参评文档版本（KB 当前活动版本快照）
    questions_json TEXT NOT NULL,         -- 问题列表（JSON 字符串数组）
    param_groups_json TEXT NOT NULL,      -- 参数组列表（[{parent,child}] JSON）
    state TEXT NOT NULL,                  -- running/completed/failed/cancelled
    current_group_index INTEGER,          -- 进度：当前参数组下标
    current_question_index INTEGER,       -- 进度：组内当前问题下标
    results_json TEXT,                    -- 各组结果快照（终态写入，组粒度增量更新）
    error_code TEXT,                      -- 失败终态的错误码
    created_at TEXT NOT NULL,             -- UTC ISO-8601
    updated_at TEXT NOT NULL              -- UTC ISO-8601
);

CREATE INDEX ix_evaluation_runs_kb ON evaluation_runs(knowledge_base_id, created_at);
