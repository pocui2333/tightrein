"""按来源确认：部署后再出现即回归；观察期满没再出现为通过；观察期内为等待；确定性来源部署后即通过；
没有关联问题的部署后即通过；整体回归优先、其次等待。"""

from datetime import timedelta

from tightrein.release.accept import confirm
from tightrein.release.accept.confirm import PASSED, REGRESSED, WAITING, ProblemCheck, decide
from tightrein.store.tables import occurrences, problems


def _problem(runtime, problem_id, source, kit):
    problems.save(runtime.conn, problems.Problem(problem_id, f"fp-{problem_id}", source, "error", "new", "t",
                                                 kit.NOW - timedelta(days=2), kit.NOW - timedelta(days=1)),
                  runtime.clock)


def _seen(runtime, problem_id, at, commit="d" * 40):
    occurrences.add(runtime.conn, occurrences.Occurrence(problem_id, at, "collect.platform_errors", commit=commit),
                    runtime.clock)


def test_observation_regresses_waits_or_passes(kit, runtime):
    deployed = kit.NOW - timedelta(hours=2)
    _problem(runtime, "P-0001", "collect.platform_errors", kit)
    _seen(runtime, "P-0001", deployed - timedelta(minutes=5))  # 部署之前的不算
    waiting = confirm.confirm(runtime.conn, ["P-0001"], deployed, kit.NOW, runtime.settings)
    assert waiting.result == WAITING and waiting.due == "2026-10-09T01:00:00Z"
    passed = confirm.confirm(runtime.conn, ["P-0001"], deployed, deployed + timedelta(hours=24), runtime.settings)
    assert passed.result == PASSED
    _seen(runtime, "P-0001", deployed + timedelta(hours=1))
    regressed = confirm.confirm(runtime.conn, ["P-0001"], deployed, kit.NOW, runtime.settings)
    assert regressed.result == REGRESSED and "部署后出现 1 次" in regressed.summary()
    assert "commit dddddddddddd" in regressed.regressions[0].detail


def test_deterministic_sources_pass_right_after_the_deploy(kit, runtime):
    _problem(runtime, "P-0002", "collect.static", kit)
    assert confirm.confirm(runtime.conn, ["P-0002"], kit.NOW, kit.NOW, runtime.settings).result == PASSED


def test_issues_without_problems_pass_on_deploy(runtime, kit):
    result = confirm.confirm(runtime.conn, [], kit.NOW, kit.NOW, runtime.settings)
    assert result.result == PASSED and result.summary() == "没有关联问题，部署后即确认"


def test_the_decision_prefers_regressions_then_waits():
    def check(result):
        return ProblemCheck("P", "s", result, "", "2026-10-09T00:00:00Z")

    assert decide((check(PASSED), check(WAITING), check(REGRESSED))) == REGRESSED
    assert decide((check(PASSED), check(WAITING))) == WAITING
    assert decide((check(PASSED),)) == PASSED


def test_criteria_confirmed_after_deploy_follow_their_problem(kit, runtime):
    """Issue 正文中部署后才能确认的验收标准按指纹对应到关联问题的确认结论；对应不上的取整体结论。"""
    deployed = kit.NOW - timedelta(hours=2)
    _problem(runtime, "P-0001", "collect.platform_errors", kit)
    _problem(runtime, "P-0002", "collect.static", kit)
    result = confirm.confirm(runtime.conn, ["P-0001", "P-0002"], deployed, kit.NOW, runtime.settings)
    criteria = ["部署后的观察期与之后的覆盖运行中不再出现指纹为 `fp-P-0001` 的问题",
                ("[ ] No problem with fingerprint `fp-P-0002` appears in the post-deploy observation window and later "
                 "covering runs"),
                "部署后的观察期与之后的覆盖运行中不再出现指纹为 `merged-away` 的问题"]
    assert [item["result"] for item in result.criteria(criteria)] == [WAITING, PASSED, WAITING]
    assert result.criteria(criteria)[0]["criterion"] == criteria[0]
