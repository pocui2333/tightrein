import json
from datetime import timedelta

import pytest
from eval_world import BEHAVIOR_FILE, TRIAGE_OUTPUTS, handoff
from replay_support import record, replay_runner

from tightrein.config.capabilities import CapabilityError
from tightrein.contracts import validate
from tightrein.domain.enums import EvalVerdict, ScoreMethod, Stage
from tightrein.evaluation import rubric, service
from tightrein.evaluation.errors import EvalCaseTampered, EvaluationRefused
from tightrein.evaluation.sandbox import SubprocessModuleRunner
from tightrein.evaluation.scorers import judge
from tightrein.evaluation.scorers.base import JudgeRequest
from tightrein.evaluation.service import Dependencies, EvaluationSettings
from tightrein.evaluation.variants import VersionSpec, version_plan
from tightrein.observability import events
from tightrein.runner.task import Subject
from tightrein.store.files.layout import WorkspaceLayout

EVALUATION = "EV-20261005-030000"
JUDGE_RUN = "R-20261005-030000-improve"
PASSED = {"items": [{"itemId": "triage.counter-check", "result": "pass", "reason": "追到了入口", "evidence": []}]}


class Bench:
    def __init__(self, world, repos, make_config, tmp_path):
        self.world = world
        self.tmp_path = tmp_path
        _, self.project = repos.origin_and_clone()
        self.commit = repos.head(self.project)
        self.case_dir = world.module_case("E-0001", input_handoff=handoff(), commit=self.commit[:12])
        world.behave(world.tool.root, {"E-0001": {"outputs": TRIAGE_OUTPUTS}})
        world.sealed()
        self.recordings = tmp_path / "recordings"
        self.config = make_config()
        self.entry = world.fake_module(tmp_path / "module")
        self.calls = 0

    def record_judge(self, output=PASSED):
        items = [item for item in rubric.load(Stage.TRIAGE).items if item.id == "triage.counter-check"]
        request = JudgeRequest(JUDGE_RUN, Stage.IMPROVE, Subject("problem", "P-0042"), "replay")
        text = (self.case_dir / "input" / "handoff.json").read_text(encoding="utf-8")
        task = judge.judge_task(request, items, self.tmp_path, text, TRIAGE_OUTPUTS)
        record(self.recordings, task, output)

    def runner_factory(self, output_dir):
        world = self.world
        return replay_runner(conn=world.conn, layout=WorkspaceLayout(world.layout.root, output_dir), tool=world.tool,
                             config=self.config, tracer=world.tracer, redactor=world.redactor,
                             environ=world.repos.environ, recordings=self.recordings)

    def deps(self, budget=100.0, judged=True, module_runner=None):
        runner = SubprocessModuleRunner({"PATH": "/usr/bin:/bin"}, entry=self.entry, timeout_seconds=30)

        class Counting:
            def run(inner, request):
                self.calls += 1
                return (module_runner or runner).run(request)

        return Dependencies(self.world.layout, self.world.tool.root,
                            EvaluationSettings(budget, self.project, "replay"), Counting(),
                            self.runner_factory if judged else None, self.world.process, self.world.clock,
                            self.world.tracer)

    def patch(self, files):
        root = self.world.tool.root
        for path, text in files.items():
            self.world.repos.write(root, path, text)
        patch = self.tmp_path / "proposal.patch"
        patch.write_text(self.world.repos.git(root, "diff"), encoding="utf-8")
        self.world.repos.git(root, "checkout", "--", ".")
        return patch


@pytest.fixture
def bench(world, repos, make_config, tmp_path):
    return Bench(world, repos, make_config, tmp_path)


def plan(candidate=VersionSpec("candidate")):
    return version_plan(Stage.TRIAGE, candidate, "claude")


def scores(world):
    return world.layout.eval_scores(EVALUATION).read_text(encoding="utf-8").splitlines()


def test_settings_take_the_judge_from_the_config_or_the_defaults(make_config):
    configured = EvaluationSettings.from_config(make_config())
    assert (configured.budget_usd, configured.judge_tool, configured.judge_model) == (20, "codex", "gpt-5")
    tiers = {tier: {"claude": {"model": f"claude-{tier}", "inputUsdPerMTok": 3, "outputUsdPerMTok": 15}}
             for tier in ("standard", "strong")}
    single_tool = make_config(evaluation=None, defaultTool="claude", capabilities=tiers, stages=None,
                              roleCapabilities={"refuter": "standard", "fix-executor": "standard"})
    defaults = EvaluationSettings.from_config(single_tool)
    assert (defaults.budget_usd, defaults.judge_tool, defaults.judge_model) == (10, "claude", None)
    with pytest.raises(CapabilityError) as caught:
        EvaluationSettings.from_config(make_config(evaluation=None))
    assert caught.value.key == "evaluation.judge.tool"


def test_the_same_version_twice_has_no_difference(bench, world):
    bench.record_judge()
    result = service.evaluate(plan(), bench.deps(), seed=7)
    assert result.verdict is EvalVerdict.PASS and result.notes == []
    assert [(item.case_id, item.variant, item.scores, item.variance) for item in result.case_stats] == [
        ("E-0001", "baseline", [1.0, 1.0, 1.0], 0.0), ("E-0001", "candidate", [1.0, 1.0, 1.0], 0.0)]
    assert [comparison.delta for comparison in result.comparisons] == [0.0]
    assert len(scores(world)) == 6 and bench.calls == 6
    data = json.loads(world.layout.eval_report_json(EVALUATION).read_text(encoding="utf-8"))
    assert validate.validate("data/eval-report.schema.json", data) == []
    assert result.report_path == world.layout.eval_report_md(EVALUATION)
    saved = json.loads(world.layout.eval_plan(EVALUATION).read_text(encoding="utf-8"))
    assert (saved["seed"], saved["plan"]["caseIds"], list(saved["caseHashes"])) == (7, ["E-0001"], ["triage/E-0001"])
    snapshot = world.layout.eval_project_snapshot(EVALUATION, bench.commit[:12])
    assert (snapshot / "src" / "OrderService.cs").is_file()
    assert (world.layout.eval_version_dir(EVALUATION, "candidate") / BEHAVIOR_FILE).is_file()
    span = [event for event in events.read(world.layout.events_log(world.clock.now().date()))
            if event.agent == "evaluation"]
    assert [(event.operation, event.decision, event.artifact) for event in span] == [
        ("run_script", "pass", f"data/evals/{EVALUATION}/report.md")]


def test_a_worse_candidate_is_rejected(bench):
    bench.record_judge()
    vague = {"E-0001": {"outputs": {**TRIAGE_OUTPUTS, "reason": "可能缺少公司过滤"}}}
    candidate = VersionSpec("IP-0003", patch=bench.patch({BEHAVIOR_FILE: json.dumps(vague, ensure_ascii=False)}))
    result = service.evaluate(plan(candidate), bench.deps(), seed=7)
    assert result.verdict is EvalVerdict.REJECT
    [comparison] = result.comparisons
    assert (comparison.baseline_mean, comparison.mean) == (1.0, pytest.approx(3 / 4))
    candidate_items = {item.item_id: item for run in result.runs if run.variant == "IP-0003" for item in run.items}
    assert candidate_items["triage.no-vague-wording"].result.value == "fail"
    assert candidate_items["triage.counter-check"].method is ScoreMethod.CODE


def test_an_interrupted_evaluation_resumes_only_missing_runs(bench, world):
    runner = SubprocessModuleRunner({"PATH": "/usr/bin:/bin"}, entry=bench.entry, timeout_seconds=30)

    class Interrupting:
        def __init__(self):
            self.count = 0

        def run(self, request):
            self.count += 1
            if self.count == 3:
                raise KeyboardInterrupt
            return runner.run(request)

    with pytest.raises(KeyboardInterrupt):
        service.evaluate(plan(), bench.deps(judged=False, module_runner=Interrupting()), seed=7)
    assert len(scores(world)) == 2
    bench.calls = 0
    result = service.resume(EVALUATION, bench.deps(judged=False))
    assert bench.calls == 4 and len(scores(world)) == 6
    assert result.verdict is EvalVerdict.PASS


def test_an_exhausted_budget_leaves_the_evaluation_incomplete(bench):
    result = service.evaluate(plan(), bench.deps(budget=0.5, judged=False), seed=7)
    assert (result.verdict, bench.calls) == (EvalVerdict.INCOMPLETE, 1)
    assert result.notes[0].startswith("累计费用 0.50 USD 已达到 evaluation.budgetUsd(0.5)")
    assert result.notes[0].endswith(f"tightrein admin eval resume {EVALUATION}")


def test_an_unavailable_tool_and_repeated_crashes_stop_the_evaluation(bench, world):
    world.behave(world.tool.root, {"E-0001": {"status": "failed", "runner": "failed", "errorType": "tool-unavailable"}})
    world.commit()
    result = service.evaluate(plan(), bench.deps(judged=False), seed=7)
    assert (result.verdict, bench.calls) == (EvalVerdict.INCOMPLETE, 1)
    assert "执行器无法启动" in result.notes[0]
    world.clock.advance(timedelta(seconds=1))
    world.behave(world.tool.root, {"E-0001": {"noHandoff": True, "runner": None, "exit": 2}})
    world.commit()
    bench.calls = 0
    crashed = service.evaluate(plan(), bench.deps(judged=False), seed=7)
    assert (crashed.verdict, bench.calls) == (EvalVerdict.INCOMPLETE, 5)
    assert "连续 3 次没有交接文档" in crashed.notes[0]


def test_tampered_cases_and_forbidden_changes_are_refused(bench, world):
    world.repos.commit(world.tool.root, "feat: 评分", {"core/tightrein/evaluation/stats.py": "SCORE = 0\n"})
    candidate = VersionSpec("IP-0003", patch=bench.patch({"core/tightrein/evaluation/stats.py": "SCORE = 1\n"}))
    with pytest.raises(EvaluationRefused, match="core/tightrein/evaluation/stats.py"):
        service.evaluate(plan(candidate), bench.deps(judged=False), seed=7)
    (bench.case_dir / "input" / "handoff.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvalCaseTampered):
        service.evaluate(plan(), bench.deps(judged=False), seed=7)
    gates = [event for event in events.read(world.layout.events_log(world.clock.now().date()))
             if event.operation == "gate" and event.decision == "reject"]
    assert len(gates) == 1 and "triage/E-0001: 内容与封存时不同" in gates[0].attributes["problems"]


def test_resume_refuses_a_changed_case_set(bench, world):
    runner = SubprocessModuleRunner({"PATH": "/usr/bin:/bin"}, entry=bench.entry, timeout_seconds=30)

    class Interrupting:
        def run(self, request):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        service.evaluate(plan(), bench.deps(judged=False, module_runner=Interrupting()), seed=7)
    world.module_case("E-0001", input_handoff=handoff(subject_id="P-0043"), commit=bench.commit[:12])
    world.sealed()
    with pytest.raises(EvalCaseTampered, match="与评测开始时的哈希不同"):
        service.resume(EVALUATION, bench.deps(judged=False, module_runner=runner))
