import pytest
from pipeline_world import make_signal
from store_problem import make_problem
from triage_world import FakeRunner, make_triage_world, verification

from tightrein.domain.enums import Complexity, Probe, RunnerStatus, ScoreResult, Verdict
from tightrein.pipeline.triage.prompts.common import PromptContext, RoleCalls
from tightrein.pipeline.triage.steps import claims, evidence, evidence_checks, refute
from tightrein.runner.task import Limits

RUN = "R-20261005-030000-triage"
CLAIM = claims.build(make_problem(), [make_signal()], [])


def calls(world, runner, retries=2):
    return RoleCalls(runner, world.clock, PromptContext(world.tool, world.config, RUN, world.worktree), retries)


def results(world, output):
    items = evidence_checks.check(evidence_checks.outputs_for(CLAIM, output), world.worktree, world.clock)
    return {item.item_id: item.result for item in items}


@pytest.mark.parametrize("change, failed", [
    ({"facts": [{"location": "src/Services/Missing.src:1", "observation": "x"}]}, "triage.evidence-location"),
    ({"facts": [{"location": "src/Services/OrderService.src:99", "observation": "x"}]}, "triage.evidence-location"),
    ({"trigger": "可能在并发时出现"}, "triage.no-vague-wording"),
    ({"verdict": "conditional", "trigger": None}, "triage.output-schema"),
    ({"counterEvidence": [{"check": "入口", "entry": "OrderController", "result": "无",
                           "upstreamValidation": {"status": "absent", "location": None}}]}, "triage.counter-check"),
    ({"counterEvidence": []}, "triage.counter-check"),
])
def test_each_check_fails_on_its_own_defect(tmp_path, change, failed):
    world = make_triage_world(tmp_path)
    assert set(results(world, verification()).values()) <= {ScoreResult.PASS, ScoreResult.NOT_APPLICABLE}
    found = results(world, verification(**change))
    assert found[failed] is ScoreResult.FAIL
    assert [item for item, result in found.items() if result is ScoreResult.FAIL] == [failed]


def test_refuted_source_by_location_or_fact_and_insufficient_needs_missing_info(tmp_path):
    world = make_triage_world(tmp_path)
    assert results(world, verification("refuted"))["triage.refuted-source"] is ScoreResult.PASS
    by_fact = verification("refuted", sourceOfPhenomenon={"location": None, "factRef": 1, "explanation": "请求非法"})
    assert results(world, by_fact)["triage.refuted-source"] is ScoreResult.PASS
    beyond = verification("refuted", sourceOfPhenomenon={"location": None, "factRef": 40, "explanation": "x"})
    assert results(world, beyond)["triage.refuted-source"] is ScoreResult.FAIL
    assert results(world, verification("insufficient"))["triage.output-schema"] is ScoreResult.PASS
    assert results(world, verification("insufficient", missingInfo=[]))["triage.output-schema"] is ScoreResult.FAIL


def test_failed_checks_are_sent_back_and_two_redos_end_unpassed(tmp_path):
    world = make_triage_world(tmp_path)
    bad = verification(trigger="大概在并发时出现")
    runner = FakeRunner({"claim-verifier": [bad, RunnerStatus.SCHEMA_INVALID, bad]})
    found = evidence.gather(calls(world, runner), "P-0001", CLAIM, role="claim-verifier",
                            complexity=Complexity.LOW, knowledge="没有与本任务相关的知识条目。")
    assert (found.passed, found.statuses) == (False, [RunnerStatus.OK, RunnerStatus.SCHEMA_INVALID, RunnerStatus.OK])
    assert found.reason.startswith("[triage.no-vague-wording]")
    assert [task.attempt for task in runner.tasks] == [1, 2, 3]
    assert "## 需要处理的问题" not in runner.tasks[0].instructions.prompt
    assert "[triage.no-vague-wording] 结论含含糊措辞" in runner.tasks[1].instructions.prompt
    assert "上一次执行没有完成：执行器返回 schema-invalid(fake-error)" in runner.tasks[2].instructions.prompt


def test_a_redo_that_passes_stops_the_loop(tmp_path):
    world = make_triage_world(tmp_path)
    runner = FakeRunner({"claim-verifier": [verification(trigger="也许会出现"), verification()]})
    found = evidence.gather(calls(world, runner), "P-0001", CLAIM, role="claim-verifier",
                            complexity=Complexity.HIGH, knowledge="-", extra=["P-0002 说入口有校验"])
    assert found.passed and found.outputs["verdict"] == "confirmed" and len(runner.tasks) == 2
    task = runner.tasks[0]
    assert (task.route, task.limits) == ("triage.claim-verifier", Limits(max_turns=100, max_duration_ms=2400000))
    assert "- P-0002 说入口有校验" in task.instructions.prompt
    assert "## 验收标准\n\n交付的结果须满足下面的验收标准：" in task.instructions.prompt
    assert "\n- 做过反证检查" in task.instructions.prompt
    assert "[triage.counter-check]" not in task.instructions.prompt and "评分表" not in task.instructions.prompt


REPORT = {"title": "[订单] 其他公司的订单查询返回 500", "summary": "查询失败。", "steps": ["查询"], "expected": "403",
          "actual": "500", "acceptance": ["返回 403"], "severity": "P2", "severityReason": "非核心"}


def test_static_problems_use_the_collected_verification_first(tmp_path):
    world = make_triage_world(tmp_path)
    without_report = make_signal(probe=Probe.STATIC, check="DP-0001", context={"verification": verification()})
    assert evidence.prepared_output(without_report) is None
    static = make_signal(probe=Probe.STATIC, check="DP-0001",
                         context={"verification": verification(report=REPORT)})
    problem = make_problem(probe=Probe.STATIC)
    assert evidence.reuses_prepared(problem) and not evidence.reuses_prepared(make_problem())
    without_assessment = verification(report=REPORT)
    del without_assessment["assessment"]
    assert evidence.prepared_output(make_signal(probe=Probe.STATIC, context={"verification": without_assessment})) \
        is None
    runner = FakeRunner({"claim-verifier": [verification()]})
    found = evidence.gather(calls(world, runner), "P-0001", CLAIM, role="claim-verifier", complexity=Complexity.LOW,
                            knowledge="-", prepared=evidence.prepared_output(static))
    assert found.passed and runner.tasks == []
    old = verification(counterEvidence=[], report=REPORT)
    found = evidence.gather(calls(world, runner), "P-0001", CLAIM, role="claim-verifier", complexity=Complexity.LOW,
                            knowledge="-", prepared=old)
    assert found.passed and runner.roles() == ["claim-verifier"]


def test_the_refuter_gets_the_same_claim_and_its_own_route(tmp_path):
    world = make_triage_world(tmp_path)
    runner = FakeRunner({"refuter": [verification()]})
    found = evidence.gather(calls(world, runner), "P-0001", CLAIM, role="refuter", complexity=Complexity.MEDIUM,
                            knowledge="-")
    task = runner.tasks[0]
    assert found.passed and (task.role, task.route) == ("refuter", "triage.refuter")
    assert task.instructions.prompt.startswith("# refuter：独立的证伪复核")
    assert CLAIM.render() in task.instructions.prompt and '"verdict"' not in task.instructions.prompt


@pytest.mark.parametrize("second, expected", [
    (Verdict.REFUTED, (Verdict.REFUTED, False)),
    (Verdict.CONFIRMED, (Verdict.CONFIRMED, True)),
    (Verdict.CONDITIONAL, (Verdict.CONDITIONAL, True)),
    (Verdict.INSUFFICIENT, (Verdict.INSUFFICIENT, True)),
    (None, (Verdict.INSUFFICIENT, True)),
])
def test_refute_combination_table(second, expected):
    assert refute.combine(Verdict.REFUTED, second) == expected


@pytest.mark.parametrize("second, expected", [
    (Verdict.CONFIRMED, (Verdict.CONFIRMED, False)),
    (Verdict.CONDITIONAL, (Verdict.CONFIRMED, False)),
    (Verdict.REFUTED, (Verdict.CONFIRMED, True)),
    (Verdict.INSUFFICIENT, (Verdict.CONFIRMED, True)),
    (None, (Verdict.CONFIRMED, True)),
])
def test_refute_of_a_confirmed_verdict(second, expected):
    assert refute.combine(Verdict.CONFIRMED, second) == expected


def test_refute_is_only_needed_for_high_risk():
    from tightrein.config.project import core_config
    from tightrein.domain.enums import ImpactKind, Severity, TaskType

    rule = refute.RefuteRule.from_config(core_config())
    plain = dict(p0=False, severity=Severity.P2, task_type=TaskType.BUG, impact_kind=ImpactKind.NON_CORE_ERROR)
    assert not refute.needed(rule, Verdict.CONFIRMED, **plain)
    assert refute.needed(rule, Verdict.CONFIRMED, **{**plain, "severity": Severity.P1})
    assert refute.needed(rule, Verdict.CONDITIONAL, **{**plain, "task_type": TaskType.SECURITY})
    assert refute.needed(rule, Verdict.CONFIRMED, **{**plain, "impact_kind": ImpactKind.AUTHORIZATION})
    assert refute.needed(rule, Verdict.REFUTED, **{**plain, "p0": True})
    assert not refute.needed(rule, Verdict.REFUTED, **plain)
    assert not refute.needed(rule, Verdict.INSUFFICIENT, **{**plain, "severity": Severity.P0})


def test_the_verifier_gets_the_severity_standard_the_project_guide_and_the_title_limit(tmp_path):
    world = make_triage_world(tmp_path, triage={"severityGuide": "个人项目，数据可重新采集"})
    runner = FakeRunner({"claim-verifier": [verification()]})
    evidence.gather(calls(world, runner), "P-0001", CLAIM, role="claim-verifier", complexity=Complexity.MEDIUM,
                    knowledge="-")
    prompt = runner.tasks[0].instructions.prompt
    assert "# 严重度标准与 Issue 报告" in prompt and "## 项目说明\n\n个人项目，数据可重新采集" in prompt
    assert "`report.title` 不超过 80 个字符" in prompt


def test_the_assessed_severity_wins_over_the_impact_mapping():
    from tightrein.config.project import core_config
    from tightrein.domain.enums import Severity
    from tightrein.pipeline.triage.steps import rating

    outputs = evidence_checks.outputs_for(CLAIM, verification(
        impact={"kind": "data-correctness", "roles": [], "data": "", "callSites": [], "consequence": "x"},
        report={"title": "t", "summary": "s", "steps": [], "expected": None, "actual": None, "acceptance": [],
                "severity": "P2", "severityReason": "个人项目，可重新采集"}))
    rated = rating.rate(core_config(), make_problem(), None, outputs, p0=False)
    assert rated.severity is Severity.P2
    outputs["report"] = None
    assert rating.rate(core_config(), make_problem(), None, outputs, p0=False).severity \
        is Severity.P0


def test_bare_file_names_are_completed_and_unknown_text_references_are_sent_back(tmp_path):
    world = make_triage_world(tmp_path)
    bare = verification(facts=[{"location": "OrderService.src:12", "observation": "见 `Gone.src:3`"}])
    runner = FakeRunner({"claim-verifier": [bare, verification()]})
    found = evidence.gather(calls(world, runner), "P-0001", CLAIM, role="claim-verifier",
                            complexity=Complexity.MEDIUM, knowledge="-")
    assert found.passed and len(runner.tasks) == 2
    assert "`Gone.src:3` 在代码快照中找不到" in runner.tasks[1].instructions.prompt
    fixed = evidence.gather(calls(world, FakeRunner({"claim-verifier": [verification(
        facts=[{"location": "OrderService.src:12", "observation": "x"}])]})), "P-0001", CLAIM,
        role="claim-verifier", complexity=Complexity.MEDIUM, knowledge="-")
    assert fixed.passed and fixed.output["facts"][0]["location"] == "src/Services/OrderService.src:12"
