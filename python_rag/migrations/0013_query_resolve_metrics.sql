-- 0013: 查询事实解析段计时
-- query_runs 后补 resolve_ms 列：上下文事实解析（活动索引切片读取与
-- 候选文本组装）位于检索段与重排段计时之间，此前未被任何分段覆盖；
-- 单列记录使该段在大知识库下的延迟可见（可空：历史行无此事实）
ALTER TABLE query_runs ADD COLUMN resolve_ms INTEGER;
