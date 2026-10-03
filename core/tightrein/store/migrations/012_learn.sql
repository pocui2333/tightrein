-- 学习：
-- stage_yield 记录每次调用实际使用的工具与模型，供按模型统计一次通过率；旧行为空。
-- 自我改进只产出建议：提案、失败归类与 improve 状态三张表删除；合入、还原提案的待确认操作删除。
-- 缺陷模式建议由缺陷变规则取代，评测用例建议没有生成方，这两类建议删除。

ALTER TABLE stage_yield ADD COLUMN tool TEXT;
ALTER TABLE stage_yield ADD COLUMN model TEXT;

DROP TABLE proposals;
DROP TABLE failures;
DROP TABLE improve_state;
DELETE FROM pending_operations WHERE kind IN ('apply-proposal', 'revert-proposal');

DELETE FROM suggestions WHERE kind IN ('defect-pattern', 'eval-case');
