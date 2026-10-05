from pathlib import Path

import pytest
from fix_world import SERVICE_PATH, make_fix_world

from tightrein.domain.enums import (
    Access,
    RegressionKind,
    RegressionResult,
    ReviewCategory,
    ReviewMode,
    RunnerStatus,
)
from tightrein.guards.diff_rules import ChangedFile
from tightrein.guards.policy import GuardSettings
from tightrein.pipeline.fix.prompts.common import FixCalls, FixPrompt
from tightrein.pipeline.fix.steps import checks, context, execute, review, triage_blockers
from tightrein.pipeline.fix.steps.checks import CheckInputs, Finding
from tightrein.pipeline.checks.project_checks import Affected, CheckCommand, CheckRun
from tightrein.pipeline.checks.regressions.runner import RegressionOutcome
from tightrein.store.files.layout import ToolLayout
from tightrein.store.repos.regressions import RegressionCheck

PLAN = {"files": [{"path": SERVICE_PATH, "isNew": False, "reason": None}], "split": None}
SETTINGS = GuardSettings(protected_paths=("src/Auth/",), test_paths=("tests/",), max_files=2, max_lines=10,
                         residue_patterns=(r"print\(",))


def evaluate(changes, untracked=(), **changes_to_inputs):
    return checks.evaluate(CheckInputs(changes, set(untracked), changes_to_inputs.pop("plan", PLAN), SETTINGS,
                                       **changes_to_inputs))


def kinds(report):
    return [(item.check, item.category) for item in report.findings]


def test_a_clean_change_passes():
    report = evaluate([ChangedFile(SERVICE_PATH, added=("return x;",), lines_added=1)])
    assert report.passed and report.findings == () and report.not_run == ()


@pytest.mark.parametrize("changes,untracked,expected", [
    ([ChangedFile("src/Other.src", lines_added=1), ChangedFile(SERVICE_PATH)], (), [
        ("outside-plan", ReviewCategory.PLAN_GAP)]),
    ([ChangedFile(SERVICE_PATH), ChangedFile("notes.txt", added=("x",))], ("notes.txt",), [
        ("temporary-file", ReviewCategory.LOCAL)]),
    ([ChangedFile(SERVICE_PATH, lines_added=11)], (), [("size", ReviewCategory.LOCAL)]),
    ([ChangedFile("src/Auth/Matrix.src")], (), [("outside-plan", ReviewCategory.PLAN_GAP),
                                                ("protected", ReviewCategory.NEEDS_USER)]),
    ([ChangedFile("tests/OrderTests.src")], (), [("outside-plan", ReviewCategory.PLAN_GAP),
                                                 ("tests", ReviewCategory.LOCAL)]),
    ([ChangedFile(SERVICE_PATH, added=("@unittest.skip",))], (), [("skip-marker", ReviewCategory.LOCAL)]),
    ([ChangedFile(SERVICE_PATH, added=("print(order)",))], (), [("residue", ReviewCategory.LOCAL)]),
])
def test_each_deterministic_check_has_its_category(changes, untracked, expected):
    assert kinds(evaluate(changes, untracked)) == expected


def test_confirmed_exceptions_and_hardcode_hints():
    big = [ChangedFile(SERVICE_PATH, lines_added=10), ChangedFile("tests/OrderTests.src", lines_added=50)]
    assert [item.check for item in evaluate(big, plan={**PLAN, "files": [
        *PLAN["files"], {"path": "tests/OrderTests.src", "isNew": False, "reason": None}]}).findings] == ["tests"]
    protected = [ChangedFile("src/Auth/Matrix.src")]
    plan = {"files": [{"path": "src/Auth/Matrix.src", "isNew": False, "reason": None}], "split": None}
    assert evaluate(protected, plan=plan, approved_protected=("src/Auth/Matrix.src",)).passed
    report = evaluate([ChangedFile(SERVICE_PATH, added=('if (id == "ORD-20261005")',))],
                      reproduction={"ORD-20261005"})
    assert report.passed and [item.detail for item in report.hardcode] == [
        "新增的字面量 ORD-20261005 与复现检查中的输入或期望值相同"]


def outcome(issue, result, detail=""):
    return RegressionOutcome(RegressionCheck(issue, "static-1", RegressionKind.STATIC, "p", "h"), result,
                             f"{SERVICE_PATH}:12", detail)


def test_project_checks_and_static_regressions():
    runs = [CheckRun("unit", "make unit a.test.js", 1, Path("logs/unit.log")),
            CheckRun("sort", "make sort-lang", 0, Path("logs/sort.log"), modified=("lang/en.js",)),
            CheckRun("build", "make", None, Path("logs/build.log"), not_run_reason="无法启动：make 不存在")]
    report = evaluate([ChangedFile(SERVICE_PATH)], project=runs,
                      static_repro=[outcome("0001", RegressionResult.FAILED, "规则仍然命中")],
                      other_regressions=[outcome("0003", RegressionResult.FAILED),
                                         outcome("0004", RegressionResult.NOT_RUN, "Semgrep 失败")])
    assert kinds(report) == [("project:unit", ReviewCategory.LOCAL), ("project:sort", ReviewCategory.LOCAL),
                             ("repro", ReviewCategory.LOCAL), ("other-regressions", ReviewCategory.LOCAL)]
    assert report.not_run == ("项目检查 build 未运行：无法启动：make 不存在；检查 checks.prepare 是否已准备好依赖",
                              "0004/static-1 未执行：Semgrep 失败")
    assert not report.passed


def test_a_restored_repro_test_is_a_local_problem():
    report = evaluate([ChangedFile(SERVICE_PATH)], repro_restored=["tests/test_order.py"])
    assert kinds(report) == [("repro-test", ReviewCategory.LOCAL)]
    assert report.findings[0].location == "tests/test_order.py"


def test_categories_are_handled_in_order():
    findings = [Finding("a", None, "x", ReviewCategory.LOCAL), Finding("b", None, "y", ReviewCategory.NEEDS_USER),
                Finding("c", None, "z", ReviewCategory.PLAN_GAP)]
    groups = triage_blockers.classify(findings)
    assert list(groups) == [ReviewCategory.NEEDS_USER, ReviewCategory.PLAN_GAP, ReviewCategory.LOCAL]
    assert triage_blockers.first(groups) is ReviewCategory.NEEDS_USER
    design = Finding("d", None, "w", ReviewCategory.DESIGN)
    assert triage_blockers.first(triage_blockers.classify([design, *findings])) is ReviewCategory.DESIGN
    assert triage_blockers.first({}) is None


def blocker(**changes):
    item = {"kind": "caller-broken", "location": f"{SERVICE_PATH}:12", "trigger": "传入空编号",
            "problem": "调用方 Cart 没有同步修改", "category": "local", "rootCause": "漏改一处调用点"}
    item.update(changes)
    return item


def test_findings_without_location_or_trigger_are_discarded(tmp_path):
    world = make_fix_world(tmp_path)
    output = {"mode": "light", "items": [], "unverified": [], "blockers": [
        blocker(), blocker(location=None), blocker(location=f"{SERVICE_PATH}:99"), blocker(trigger=" "),
        blocker(kind="authz"), blocker(kind="style")]}
    kept, discarded = review.filter_findings(output, world.worktree, ReviewMode.LIGHT)
    assert kept == [blocker()]
    assert [item["reason"] for item in discarded] == [
        "没有给出「文件路径:行号」", f"位置不存在：{SERVICE_PATH} 只有 40 行，引用了第 99 行", "没有给出触发条件",
        "问题类别 authz 不在轻量评审允许的范围内", "问题类别 style 不在轻量评审允许的范围内"]
    assert review.filter_findings({"blockers": [blocker(kind="authz")]}, world.worktree, ReviewMode.DEEP)[0]


def test_reviews_run_in_the_given_mode_with_the_results_and_unknown_items_block(tmp_path):
    world = make_fix_world(tmp_path)
    ctx = context.load(world.conn, world.layout, world.issue_id)
    calls = FixCalls(world.runner, world.clock, FixPrompt(ToolLayout(), world.config, "R-20261005-030000-fix",
                                                          world.worktree))
    unknown = {"mode": "deep", "blockers": [], "unverified": [],
               "items": [{"itemId": "fix.acceptance", "result": "unknown", "reason": "看不到数据"}]}
    world.runner.add("fix-reviewer", unknown)
    result = review.review(calls, ctx, {"summary": "x"}, "+x", [], ReviewMode.DEEP, "复现测试：passed", 1)
    assert (result.mode, result.passed, world.runner.roles()) == (ReviewMode.DEEP, False, ["fix-reviewer-deep"])
    prompt = world.runner.tasks[0].instructions.prompt
    assert "## 实际结果(第 7 步)\n\n复现测试：passed" in prompt and "## 特判检查" in prompt
    world.runner.outputs["fix-reviewer"] = [{"mode": "light", "blockers": [blocker()], "items": [], "unverified": []}]
    result = review.review(calls, ctx, {"summary": "x"}, "+x", [], ReviewMode.LIGHT, "", 2)
    assert result.mode is ReviewMode.LIGHT and [item.category for item in result.findings()] == [ReviewCategory.LOCAL]


def test_the_executor_writes_only_its_worktree_within_the_budget(tmp_path):
    world = make_fix_world(tmp_path, stages={"fix": {"budget": {"low": 1.5, "high": 4}}})
    ctx = context.load(world.conn, world.layout, world.issue_id)
    calls = FixCalls(world.runner, world.clock, FixPrompt(ToolLayout(), world.config, "R-1", world.worktree))
    world.runner.add("fix-executor", RunnerStatus.GUARD_VIOLATION)
    commands = [CheckCommand("unit", "web", "make unit", affected=Affected((("a", "b"),), "make unit {tests}"))]
    found = execute.execute(calls, ctx, {"summary": "x"}, 1, commands=commands,
                            approved_protected=("src/Auth/Matrix.src",), budget=1.5, corrections=["[residue] 删除"])
    task = world.runner.tasks[0]
    assert (task.access, task.workdir, task.limits.max_cost_usd) == (Access.WORKSPACE_WRITE, world.worktree, 1.5)
    assert task.allowed_commands[:1] == ("make unit",) and "git log" in task.allowed_commands
    assert task.approved_protected_paths == ("src/Auth/Matrix.src",)
    assert "## 按检查与评审意见修改" in task.instructions.prompt and found.status is RunnerStatus.GUARD_VIOLATION
