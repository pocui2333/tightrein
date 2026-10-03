-- 分诊结论的紧迫度(design 3.2)：worth-assessor 给出的 urgency，immediate 表示数据在持续写坏或有安全漏洞，
-- Issue 列表与运行摘要把这类问题排在优先分之前；没有给出紧迫度的结论为空。

ALTER TABLE triage_results ADD COLUMN urgency TEXT;
