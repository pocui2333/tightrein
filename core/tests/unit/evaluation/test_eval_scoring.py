import pytest
from eval_world import RUN, TRIAGE_OUTPUTS, handoff
from replay_support import record

from tightrein.domain.enums import ScoreMethod, ScoreResult, Stage
from tightrein.evaluation import rubric
from tightrein.evaluation.cases import load_module_case
from tightrein.evaluation.scorers import judge
from tightrein.evaluation.scorers.base import ItemResult, JudgeRequest, ScoringContext
from tightrein.evaluation.scoring import NO_JUDGE, NOT_APPLICABLE_CONDITION, failed_run, score_output, summarize
from tightrein.runner.task import Subject

REQUEST = JudgeRequest(RUN, Stage.IMPROVE, Subject("problem", "P-0042"), tool="replay")
JUDGED = {"items": [{"itemId": "fix.no-special-case", "result": "pass", "reason": "没有针对复现输入的分支",
                     "evidence": ["src/OrderService.cs:1"]},
                    {"itemId": "fix.acceptance", "result": "pass", "reason": "负数分页参数返回 400", "evidence": []}]}


def judge_items(stage=Stage.FIX):
    return [item for item in rubric.load(stage).items if item.method is ScoreMethod.JUDGE
            and item.applies_when is None]


def case_for(world, **changes):
    directory = world.module_case("E-0001", input_handoff=handoff(), **changes)
    case, problems = load_module_case(world.layout, directory, rubric.item_ids)
    assert problems == []
    return case


def context(world, case, snapshot):
    return ScoringContext(case=case, project_snapshot=snapshot, judge=REQUEST)


def results(items):
    return [(item.item_id, item.result.value) for item in items]


def recorded_judge(world, make_config, tmp_path, snapshot, case, output, status="ok", items=None):
    task = judge.judge_task(REQUEST, items or judge_items(), snapshot, case.input_file.read_text(encoding="utf-8"),
                            TRIAGE_OUTPUTS)
    record(tmp_path / "recordings", task, output, status)
    return world.runner(make_config(), tmp_path / "recordings")


def test_a_triage_output_is_scored_by_code_and_assertions(world):
    snapshot = world.snapshot()
    case = case_for(world, expected={
        "excludeItems": [{"itemId": "triage.no-vague-wording", "reason": "这个用例的结论引用了原文"}],
        "assertions": [{"path": "verdict", "op": "equals", "value": "confirmed", "description": "判定为成立"},
                       {"path": "labels[*]", "op": "equals", "value": "discuss-with-author", "description": "需要先与代码作者讨论"}]})
    scored = score_output(Stage.TRIAGE, handoff(TRIAGE_OUTPUTS), context(world, case, snapshot), None, world.clock)
    assert results(scored) == [
        ("triage.evidence-location", "pass"), ("triage.output-schema", "pass"),
        ("triage.refuted-source", "not-applicable"), ("triage.counter-check", "pass"),
        ("triage.no-vague-wording", "not-applicable"), ("assert-1", "pass"), ("assert-2", "fail")]
    assert scored[4].reason == "用例排除：这个用例的结论引用了原文"
    assert summarize(scored) == (4 / 5, False)


def test_judge_items_are_scored_by_the_recorded_judge(world, make_config, tmp_path):
    snapshot = world.snapshot()
    case = case_for(world)
    runner = recorded_judge(world, make_config, tmp_path, snapshot, case, JUDGED)
    scored = score_output(Stage.FIX, handoff(TRIAGE_OUTPUTS), context(world, case, snapshot), runner, world.clock)
    judged = [item for item in scored if item.method is ScoreMethod.JUDGE]
    assert judged[0] == ItemResult("fix.no-special-case", ScoreMethod.JUDGE, ScoreResult.PASS, "没有针对复现输入的分支",
                                   ("src/OrderService.cs:1",))
    assert [(item.item_id, item.result) for item in judged] == [
        ("fix.no-special-case", ScoreResult.PASS), ("fix.acceptance", ScoreResult.PASS),
        ("fix.deep-review", ScoreResult.NOT_APPLICABLE)]


def test_the_judge_task_sees_outputs_but_not_the_generator_session(world):
    snapshot = world.snapshot()
    task = judge.judge_task(REQUEST, judge_items(), snapshot, "输入的交接文档", TRIAGE_OUTPUTS)
    assert (task.role, task.output_schema, task.access.value, task.workdir, task.route, task.stage) == (
        "judge", "runner/roles/judge.schema.json", "read-only", snapshot, "eval.judge", Stage.IMPROVE)
    prompt = task.instructions.prompt
    assert prompt.startswith("你是独立评审。") and "不因为输出更长、写得更多而加分" in prompt
    assert "- [fix.no-special-case] " in prompt
    assert "## 运行的输入\n\n输入的交接文档" in prompt and '"verdict": "confirmed"' in prompt
    assert "transcript" not in prompt and task.instructions.skills == ()


def test_invalid_judge_output_and_missing_items_are_unknown(world, make_config, tmp_path):
    snapshot = world.snapshot()
    case = case_for(world)
    runner = recorded_judge(world, make_config, tmp_path, snapshot, case, None, status="schema-invalid")
    scored = score_output(Stage.FIX, handoff(TRIAGE_OUTPUTS), context(world, case, snapshot), runner, world.clock)
    assert [(item.item_id, item.reason) for item in scored if item.method is ScoreMethod.JUDGE
            and item.result is ScoreResult.UNKNOWN] == [("fix.no-special-case", "评审输出无效"),
                                                        ("fix.acceptance", "评审输出无效")]


def test_a_judge_answer_can_be_unknown_or_missing(world, make_config, tmp_path):
    snapshot = world.snapshot()
    items = judge_items(Stage.FIX)
    task = judge.judge_task(REQUEST, items, snapshot, None, {})
    record(tmp_path / "recordings", task, {"items": [
        {"itemId": "fix.no-special-case", "result": "unknown", "reason": "看不到复现输入", "evidence": []}]})
    judged = judge.run_judge(world.runner(make_config(), tmp_path / "recordings"), world.clock, task, items, "replay")
    assert [(item.item_id, item.result.value, item.reason) for item in judged] == [
        ("fix.no-special-case", "unknown", "看不到复现输入"), ("fix.acceptance", "unknown", "评审没有给出该项")]


def test_without_a_judge_runner_judge_items_are_unknown(world):
    snapshot = world.snapshot()
    scored = score_output(Stage.FIX, handoff(TRIAGE_OUTPUTS), ScoringContext(project_snapshot=snapshot), None,
                          world.clock)
    judged = [item for item in scored if item.method is ScoreMethod.JUDGE]
    assert [(item.item_id, item.result, item.reason) for item in judged] == [
        ("fix.no-special-case", ScoreResult.UNKNOWN, NO_JUDGE), ("fix.acceptance", ScoreResult.UNKNOWN, NO_JUDGE),
        ("fix.deep-review", ScoreResult.NOT_APPLICABLE, NOT_APPLICABLE_CONDITION)]


def test_refuted_triage_outputs_are_checked_by_code(world):
    snapshot = world.snapshot()
    refuted = {**TRIAGE_OUTPUTS, "verdict": "refuted", "reason": "可能是误报"}
    scored = score_output(Stage.TRIAGE, handoff(refuted), ScoringContext(project_snapshot=snapshot), None, world.clock)
    assert results(scored) == [
        ("triage.evidence-location", "pass"), ("triage.output-schema", "pass"), ("triage.refuted-source", "fail"),
        ("triage.counter-check", "pass"), ("triage.no-vague-wording", "fail")]
    assert summarize(scored) == (3 / 5, False)


def test_failed_runs_fail_every_applicable_item(world):
    case = case_for(world, expected={
        "excludeItems": [{"itemId": "triage.counter-check", "reason": "不适用"}],
        "assertions": [{"path": "verdict", "op": "exists", "description": "给出了判定"}]})
    scored = failed_run(Stage.TRIAGE, None, ScoringContext(case=case), "没有交接文档：退出码 1")
    assert results(scored) == [
        ("triage.evidence-location", "fail"), ("triage.output-schema", "fail"), ("triage.refuted-source", "fail"),
        ("triage.counter-check", "not-applicable"), ("triage.no-vague-wording", "fail"), ("assert-1", "fail")]
    blocked = failed_run(Stage.TRIAGE, handoff(TRIAGE_OUTPUTS, status="blocked"), ScoringContext(),
                         "交接文档为 blocked")
    assert ("triage.refuted-source", "not-applicable") in results(blocked)
    assert {item.reason for item in blocked if item.result is ScoreResult.FAIL} == {"交接文档为 blocked"}
    assert summarize(scored) == (0.0, False)


@pytest.mark.parametrize("values, expected", [
    ([], (0.0, False)),
    (["pass", "pass", "not-applicable"], (1.0, True)),
    (["pass", "unknown"], (1.0, False)),
    (["unknown"], (0.0, False)),
    (["pass", "fail", "fail", "unknown"], (1 / 3, False)),
])
def test_summary_leaves_unknown_out_of_the_denominator(values, expected):
    items = [ItemResult(f"x.{index}", ScoreMethod.CODE, ScoreResult(value), "") for index, value in enumerate(values)]
    assert summarize(items) == expected
