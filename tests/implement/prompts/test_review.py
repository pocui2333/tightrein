from __future__ import annotations

from typing import Any

from tightrein.implement.context import Decision, Risk
from tightrein.implement.prompts import review
from tightrein.prompts.build import PROMPTS_DIR

PLAN = {"summary": "改为最近 7 天", "files": [{"path": "src/orders.py"}], "steps": ["改查询"],
        "hypothesis": {"cause": "查询条件写成当天", "edits": ["src/orders.py:2"], "evidence": ["日志里只有当天"]},
        "userDecisions": [{"question": "分页要不要跟着改成每页 50 条", "recommendation": "改成 50", "reason": "7 天的量大"}]}
MERGED = review.MergedMain("a" * 40, ("tests/test_database.py",), ("aaaaaaaaaaaa test: 补连接池的测试",),
                           "diff --git a/tests/test_database.py b/tests/test_database.py\n+def test_pool_reuse():\n",
                           resolved_by_code=True)
CHECK = {"counted": {"files": 1, "lines": 2},
         "commands": [{"command": "pytest -q", "result": "failed", "reason": "退出码 1", "output": "E   assert 1 == 7"}],
         "runtime": [{"id": "api:shallow", "result": "weak", "reason": "没有数据"}]}


def test_light_review_gets_the_actual_results_and_a_trimmed_plan(world: Any) -> None:
    variables = review.light(world.context(), plan=PLAN, diff="diff --git a/x b/x", results=review.results_text(CHECK),
                             hints=["`7` 与测试输入相同"], previous=[], files=None)
    assert set(variables) == {"issue", "acceptance", "notes", "plan", "decisions", "merge", "scope", "previous",
                              "diff", "results", "hints"}
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
    assert set(variables) == {"acceptance", "decisions", "merge", "risk", "diff", "results"}
    assert "schema：含数据库迁移" in variables["risk"]
    assert "订单列表只显示当天" not in "".join(variables.values())


def test_both_reviews_get_the_user_decisions(world: Any) -> None:
    """用户的决定高于方案：轻量与深度审查都拿到它(深度审查虽是盲审，用户的要求不属于编码的思路)。"""
    decision = Decision("implement.approve", "approve", None, "分页保持每页 20 条，不按方案改成 50", "2026-10-08T03:00:00Z")
    context = world.context(decisions=[decision])
    light = review.light(context, plan=PLAN, diff="", results="", hints=[], previous=[], files=None)
    deep = review.deep(context, diff="", results="", risk=None)
    for variables in (light, deep):
        assert "分页保持每页 20 条" in variables["decisions"]
    prompts = PROMPTS_DIR / "implement.review.md", PROMPTS_DIR / "implement.review.deep.md"
    for text in (path.read_text(encoding="utf-8") for path in prompts):
        assert "{{decisions}}" in text and "高于方案" in text
    assert "高于方案" in (PROMPTS_DIR / "implement.code.md").read_text(encoding="utf-8")


def test_the_light_review_gets_the_questions_the_user_answered(world: Any) -> None:
    """用户的决定只记了回答：方案交给用户拍板的问题(userDecisions)随方案摘要给审查，才核对得了照做没有。"""
    variables = review.light(world.context(), plan=PLAN, diff="", results="", hints=[], previous=[], files=None)
    assert "分页要不要跟着改成每页 50 条" in variables["plan"] and "改成 50" in variables["plan"]


def test_both_reviews_get_the_merged_main(world: Any) -> None:
    """交付合并过 main 时两种审查都拿到两侧都改了的文件、main 一侧的提交与改动(当作数据包住)；没合并时为「无」。"""
    light = review.light(world.context(), plan=PLAN, diff="", results="", hints=[], previous=[], files=None,
                         merge=MERGED)
    deep = review.deep(world.context(), diff="", results="", risk=None, merge=MERGED)
    for variables in (light, deep):
        text = variables["merge"]
        assert "tests/test_database.py" in text and "aaaaaaaaaaaa test: 补连接池的测试" in text
        assert '<external source="main 一侧的改动">' in text and "+def test_pool_reuse():" in text
        assert "合并时冲突，由编码解决" in text and "a" * 12 in text and "a" * 13 not in text
    assert review.light(world.context(), plan=PLAN, diff="", results="", hints=[], previous=[],
                        files=None)["merge"] == "无"
    assert review.deep(world.context(), diff="", results="", risk=None)["merge"] == "无"
    auto = review.merge_text(review.MergedMain("b" * 40, ("src/a.py",), (), "", resolved_by_code=False))
    assert "发布合并时 git 自动合并" in auto and "提交：无" in auto and "(没有改动)" in auto
    assert "人在发布阶段" not in auto
    prompts = PROMPTS_DIR / "implement.review.md", PROMPTS_DIR / "implement.review.deep.md"
    for text in (path.read_text(encoding="utf-8") for path in prompts):
        assert "## 合并进来的 main\n\n{{merge}}" in text
        assert "main 一侧的改动" in text and "必须仍然在" in text and "除非用户的决定要求取舍" in text
        assert "写明是有意舍弃的(人在发布阶段解决冲突时的取舍、用户在编码关卡上选保留的一侧，都与用户的决定一样作数)" in text
        assert "报 `main-dropped`" in text


def test_the_sides_a_person_chose_at_release_are_named(world: Any) -> None:
    """人在发布阶段解决冲突时的取舍与用户的决定一样作数：逐个文件写明取了哪一侧；两侧合并的指向人的合并提交。"""
    text = review.merge_text(review.MergedMain(
        "c" * 40, ("tests/test_a.py", "src/b.py", "src/c.py", "src/d.py"), (), "", resolved_by_code=False,
        sides={"tests/test_a.py": "fix-side", "src/b.py": "main-side", "src/c.py": "both"}, merged="d" * 40))
    assert "发布合并时合并，人解决过冲突的见下一条" in text
    assert "人在发布阶段解决冲突时的取舍(与用户的决定一样作数)：tests/test_a.py 整份取了修复一侧" in text
    assert "src/b.py 整份取了 main 一侧：修复一侧原来对它的改动是人有意舍弃的" in text
    assert f"src/c.py 两侧合并：人合并的结果在提交 {'d' * 12}" in text and f"`git show {'d' * 12}:src/c.py`" in text
    assert "src/d.py" not in text.split("人在发布阶段")[1].split("\n")[0]  # git 自动合并的不算人的取舍


def test_the_side_the_user_chose_at_the_coding_gate_is_named(world: Any) -> None:
    """用户在编码关卡上为改文件解决不了的冲突选的一侧：用户的决定里只是「implement.code，选项 1」，这里写明它是用户的
    决定、对应哪个文件；留了冲突标记、由编码解决的文件不在其中。"""
    text = review.merge_text(review.MergedMain("c" * 40, ("assets/logo.bin", "src/b.py"), (), "", resolved_by_code=True,
                                               sides={"assets/logo.bin": "fix-side"}))
    assert "两侧都改了的文件(合并时冲突，由编码解决；改文件解决不了的按用户选的一侧写成，见下一条)" in text
    chosen = text.split("用户在编码关卡上为改文件解决不了的冲突选的一侧")[1].split("\n")[0]
    assert "implement.code 关卡上的选项 1 为修复一侧、2 为 main 一侧" in chosen
    assert "assets/logo.bin 整份取了修复一侧" in chosen and "src/b.py" not in chosen
    assert "人在发布阶段" not in text


def test_the_deep_review_gets_only_the_questions_from_the_plan(world: Any) -> None:
    """深度审查拿不到方案：用户的回答(如「第二点按 B」)只有对着问题才看得懂，方案交给用户拍板的问题与推荐附在用户的
    决定之后；方案的其他内容(推荐的理由、摘要)不给。没有用户的决定时不附。"""
    decision = Decision("implement.approve", "approve", None, "同意，分页按 B", "2026-10-08T03:00:00Z")
    deep = review.deep(world.context(decisions=[decision]), diff="", results="", risk=None,
                       questions=PLAN["userDecisions"])
    assert "同意，分页按 B" in deep["decisions"]
    assert "- 分页要不要跟着改成每页 50 条(方案推荐：改成 50)" in deep["decisions"]
    assert "7 天的量大" not in "".join(deep.values()) and "改为最近 7 天" not in "".join(deep.values())
    assert review.deep(world.context(), diff="", results="", risk=None,
                       questions=PLAN["userDecisions"])["decisions"] == "无"
    text = (PROMPTS_DIR / "implement.review.deep.md").read_text(encoding="utf-8")
    assert "其后附了方案当时交给用户拍板的问题与推荐" in text


def test_unfollowed_decisions_have_their_own_kind_and_category(world: Any) -> None:
    """没照用户的决定做不是「验收标准没达成」：单独的 decision-unmet，局部问题；照做要改方案外的文件才是方案缺口。"""
    prompts = PROMPTS_DIR / "implement.review.md", PROMPTS_DIR / "implement.review.deep.md"
    light, deep = (path.read_text(encoding="utf-8") for path in prompts)
    for text in (light, deep):
        assert "报 `decision-unmet`" in text and "包括照用户的决定做要改方案外的文件" in text
        assert "报 `requirement-unmet`，`category` 写 `local`" not in text
    assert "`requirement-unmet` 验收标准没达成" in light and "`decision-unmet` 没照用户的决定做" in light
    assert "`local`(漏改一处调用点、没照用户的决定做、丢掉了 main 一侧这类" in light
    assert "`requirement-unmet`(验收标准没达成)" in deep and "`decision-unmet`(没照用户的决定做)" in deep


def test_the_review_templates_name_the_two_kinds_hardcode_root_cause_and_categories() -> None:
    """审查只报影响正确性与不满足需求两类；逐处查特判；对照根因假说；每条意见带性质。"""
    prompts = PROMPTS_DIR / "implement.review.md", PROMPTS_DIR / "implement.review.deep.md"
    light, deep = (path.read_text(encoding="utf-8") for path in prompts)
    assert "只报两类问题：影响正确性的" in light and "不满足需求的" in light
    for text in (light, deep):
        for kind in ("root-cause-unfixed", "caller-broken", "hardcode", "new-error-path", "main-dropped",
                     "requirement-unmet", "requirement-reduced", "decision-unmet"):
            assert f"`{kind}`" in text
        assert "没有具体触发条件的假想风险不报" in text
        for category in ("`local`", "`plan_gap`", "`needs_user`", "`design`"):
            assert category in text
    assert "专门检查特判" in light and "按输入值、编号、固定字符串分支" in light
    assert "对照根因假说" in light and "修改位置没改到" in light and "多出来的改动本身不算问题" in light
