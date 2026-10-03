-- 分诊标签「建议优先修复」(prioritize)由处理标签(treatment)取代：从已有分诊结果的 labels 中去掉。

UPDATE triage_results
SET labels = (SELECT json_group_array(value) FROM json_each(triage_results.labels) WHERE value != 'prioritize')
WHERE EXISTS (SELECT 1 FROM json_each(triage_results.labels) WHERE value = 'prioritize');
