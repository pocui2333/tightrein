"""评估的三份提示中防止误判的规则(原角色说明里的条目，迁移后只在提示文字里，用断言守住)。"""

import json
from pathlib import Path

import pytest

from tightrein.prompts.build import PROMPTS_DIR, check_template

ASSESS_DIR = PROMPTS_DIR.parent / "assess"


def text(point: str) -> str:
    return (PROMPTS_DIR / f"{point}.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("point", ["assess.triage", "assess.refute", "assess.dedup"])
def test_the_templates_follow_the_template_rules(point):
    assert check_template(PROMPTS_DIR / f"{point}.md") == []
    assert "{{feedback}}" in text(point)  # 重做时把上一次没通过的逐项原因列在「需要处理的问题」下


SCORING_WORDS = ("分数", "得分", "打分", "评分标准", "score", "rubric")


@pytest.mark.parametrize("point", ["assess.triage", "assess.refute", "assess.dedup", "implement.design",
                                   "implement.design.frontend", "implement.code", "implement.code.continue"])
def test_generators_see_criteria_as_items_without_any_scoring(point):
    """验收标准给生成者看的是条目文字，不出现评分、分数的说法(只有对错，没有分数可凑)。"""
    found = text(point).lower()
    assert [word for word in SCORING_WORDS if word in found] == []


def test_the_verifier_walks_the_four_steps_and_traces_to_the_entry():
    found = text("assess.triage")
    assert "四步" in found and "沿调用链追到入口" in found
    assert "不确定时选 `insufficient`" in found and "`source` 标 `user`" in found and "不会向任何人提问" in found
    assert "`analysis`：先写" in found
    assert "{{severity_guide}}" in found and "{{title_limit}}" in found and "以项目说明为准" in found
    assert "不按类别套用" in found and "能绕开的降一级" in found
    assert "不写「现有测试全部通过」" in found  # 验收标准只写这个问题特有的


def test_the_refuter_tries_to_refute_and_admits_what_it_cannot():
    found = text("assess.refute")
    assert "专门找理由反驳" in found and "如实承认反驳不了的地方" in found
    assert "换角色、换数据状态、换时序、换入口" in found
    assert "只在前端挡住、接口本身没挡的不算反证" in found
    assert "看不到第一次的判定" in found


def test_dedup_prefers_different_when_unsure():
    found = text("assess.dedup")
    assert "现象相似不等于同一根因" in found and "拿不准时判为不同" in found


def test_the_output_schema_requires_the_free_analysis_first():
    schema = json.loads((ASSESS_DIR / "assess.triage.schema.json").read_text(encoding="utf-8"))
    assert schema["required"][0] == "analysis" and "knowledgeSuggestions" in schema["required"]
    assert Path(ASSESS_DIR / "assess.dedup.schema.json").is_file()
