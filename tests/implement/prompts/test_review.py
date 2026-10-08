from __future__ import annotations

from typing import Any

from tightrein.implement.context import Risk
from tightrein.implement.prompts import review
from tightrein.prompts.build import PROMPTS_DIR

PLAN = {"summary": "改为最近 7 天", "files": [{"path": "src/orders.py"}], "steps": ["改查询"],
        "hypothesis": {"cause": "查询条件写成当天", "edits": ["src/orders.py:2"], "evidence": ["日志里只有当天"]}}
CHECK = {"counted": {"files": 1, "lines": 2},
         "commands": [{"command": "pytest -q", "result": "failed", "reason": "退出码 1", "output": "E   assert 1 == 7"}],
         "runtime": [{"id": "api:shallow", "result": "weak", "reason": "没有数据"}]}


def test_light_review_gets_the_actual_results_and_a_trimmed_plan(world: Any) -> None:
    variables = review.light(world.context(), plan=PLAN, diff="diff --git a/x b/x", results=review.results_text(CHECK),
                             hints=["`7` 与测试输入相同"], previous=[], files=None)
    assert set(variables) == {"issue", "acceptance", "notes", "plan", "scope", "previous", "diff", "results", "hints"}
    assert variables["scope"] == review.FULL_SCOPE
    assert "列表显示最近 7 天的订单" in variables["acceptance"]
    # 根因假说只留因果链与修改位置；编码用的步骤不给审查
    assert "查询条件写成当天" in variables["plan"] and "日志里只有当天" not in variables["plan"]
    assert "改查询" not in variables["plan"]
    assert variables["diff"].startswith("<external")
    results = variables["results"]
    assert "1 个文件、2 行" in results and "E   assert 1 == 7" in results and "api:shallow：weak(没有数据)" in results


def test_correction_rounds_name_the_files_and_the_previous_problems(world: Any) -> None:
    variables = review.light(world.context(), plan={}, diff="", results="", hints=[],
                             previous=["[implement.review/hardcode] src/orders.py:2：写死了 7"], files=["src/orders.py"])
    assert "src/orders.py" in variables["scope"] and "上一轮的问题" in variables["scope"]
    assert "写死了 7" in variables["previous"]
    assert variables["plan"] == "无" and variables["results"] == "无" and "(没有改动)" in variables["diff"]


def test_the_deep_review_is_blind(world: Any) -> None:
    risk = Risk(True, ("schema：含数据库迁移",))
    variables = review.deep(world.context(), diff="diff --git a/x b/x", results="- ok", risk=risk)
    assert set(variables) == {"acceptance", "risk", "diff", "results"}
    assert "schema：含数据库迁移" in variables["risk"]
    assert "订单列表只显示当天" not in "".join(variables.values())


def test_the_review_templates_name_the_two_kinds_hardcode_root_cause_and_categories() -> None:
    """审查只报影响正确性与不满足需求两类；逐处查特判；对照根因假说；每条意见带性质。"""
    prompts = PROMPTS_DIR / "implement.review.md", PROMPTS_DIR / "implement.review.deep.md"
    light, deep = (path.read_text(encoding="utf-8") for path in prompts)
    assert "只报两类问题：影响正确性的" in light and "不满足需求的" in light
    for text in (light, deep):
        for kind in ("root-cause-unfixed", "caller-broken", "hardcode", "new-error-path", "requirement-unmet",
                     "requirement-reduced"):
            assert f"`{kind}`" in text
        assert "没有具体触发条件的假想风险不报" in text
        for category in ("`local`", "`plan_gap`", "`needs_user`", "`design`"):
            assert category in text
    assert "专门检查特判" in light and "按输入值、编号、固定字符串分支" in light
    assert "对照根因假说" in light and "修改位置没改到" in light and "多出来的改动本身不算问题" in light
