-- 分诊的处理标签取代优先分(redesign/03-triage.md)：删去优先分、可修复度与紧迫度，增加处理标签、任务类型与规模档；
-- Issue 索引同步。已有的分诊结论与 Issue 没有这三项，为空；Issue 文件中的旧字段读取时忽略。

ALTER TABLE triage_results DROP COLUMN priority_score;
ALTER TABLE triage_results DROP COLUMN fixability;
ALTER TABLE triage_results DROP COLUMN urgency;
ALTER TABLE triage_results ADD COLUMN treatment TEXT;
ALTER TABLE triage_results ADD COLUMN task_type TEXT;
ALTER TABLE triage_results ADD COLUMN size_tier TEXT;

ALTER TABLE issues DROP COLUMN priority_score;
ALTER TABLE issues DROP COLUMN fixability;
ALTER TABLE issues ADD COLUMN treatment TEXT;
ALTER TABLE issues ADD COLUMN task_type TEXT;
ALTER TABLE issues ADD COLUMN size_tier TEXT;
