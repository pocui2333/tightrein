-- Issue 的来源(design 4.3)：triage 为分诊结论生成，manual 为用户直接提出的需求(不关联问题与信号，没有复现检查)。
-- 已有的 Issue 都由分诊生成。

ALTER TABLE issues ADD COLUMN origin TEXT NOT NULL DEFAULT 'triage';
