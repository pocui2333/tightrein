from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from pipeline_world import NOW, make_signal, save_issue
from store_problem import make_problem
from triage_world import FakeRunner, assessment, make_triage_world, store_problem, verification

from tightrein.config.project import core_config
from tightrein.domain.enums import (
    Complexity,
    Disposition,
    IssueStatus,
    Probe,
    RunnerStatus,
    Severity,
    SizeTier,
    Stage,
    TaskType,
    Verdict,
    YieldOutcome,
)
from tightrein.domain.problem import ProblemScope
from tightrein.domain.triage import RootCause, TriageResult
from tightrein.runner.roles import RoleSetting, read_only_task
from tightrein.pipeline.triage.prompts.common import PromptContext, RoleCalls
from tightrein.pipeline.triage.steps import attribution, claims, dedup, evidence_checks, rating
from tightrein.pipeline.triage.steps.tradeoff import tradeoff_valid
from tightrein.runner.result import RunnerResult
from tightrein.runner.task import Subject
from tightrein.store.repos import issues, stage_yield, triage
from tightrein.store.repos.issues import IssueRecord
from tightrein.store.repos.triage import TriageRecord
from tightrein.vcs.errors import GitCommandError
from tightrein.vcs.parse import BlameLine, PullState

RUN = "R-20261005-030000-triage"
FILES = ("src/Services/OrderService.src",)


def calls(world, runner):
    return RoleCalls(runner, world.clock, PromptContext(world.tool, world.config, RUN, world.worktree), 2)


def test_role_calls_with_a_connection_record_each_invoked_call(tmp_path):
    world = make_triage_world(tmp_path)
    runner = FakeRunner({"claim-verifier": [verification()]})
    recorded = RoleCalls(runner, world.clock, PromptContext(world.tool, world.config, RUN, world.worktree), 2,
                         conn=world.conn)
    recorded.run(_task("P-0001"))
    calls(world, runner).run(_task("P-0002"))
    rows = stage_yield.find(world.conn)
    assert [(row.stage, row.role, row.subject_id, row.runner_status, row.input_tokens, row.outcome)
            for row in rows] == [(Stage.TRIAGE, "claim-verifier", "P-0001", RunnerStatus.OK, 100,
                                  YieldOutcome.PENDING)]


def test_a_call_that_never_started_is_not_recorded(tmp_path):
    world = make_triage_world(tmp_path)

    class BudgetSpent:
        def run(self, task, **_):
            return RunnerResult(RunnerStatus.LIMIT_REACHED, "fake", error_type="daily-budget")

    RoleCalls(BudgetSpent(), world.clock, PromptContext(world.tool, world.config, RUN, world.worktree), 2,
              conn=world.conn).run(_task("P-0001"))
    assert stage_yield.find(world.conn) == []


def _task(problem_id):
    return read_only_task(run_id=RUN, stage=Stage.TRIAGE, role="claim-verifier", subject=Subject("problem", problem_id),
                          attempt=1, prompt="取证", workdir=Path("/tmp"),
                          output_schema="runner/roles/claim-verifier.schema.json", role_setting=RoleSetting())


def triaged(world, problem_id, at, causes=(RootCause("src/Services/OrderService.src", 12, "OrderService.Get"),)):
    triage.save(world.conn, TriageRecord(TriageResult(problem_id, 1, Verdict.CONFIRMED, Disposition.CREATE_ISSUE,
                                                      "入口没有校验", "c" * 40, root_causes=causes),
                                         "R-20261001-030000-triage", at))


def test_candidates_are_open_issues_and_recent_triaged_problems_at_the_same_place(tmp_path):
    world = make_triage_world(tmp_path)
    problem = store_problem(world, "P-0001", scope=ProblemScope("GET /api/Other"))
    store_problem(world, "P-0002", make_signal(2), scope=ProblemScope("GET /api/Other"))
    store_problem(world, "P-0003", make_signal(3), scope=ProblemScope("GET /api/Else"))
    store_problem(world, "P-0004", make_signal(4), scope=ProblemScope("GET /api/Old"))
    store_problem(world, "P-0005", make_signal(5), scope=ProblemScope("GET /api/Unrelated"))
    triaged(world, "P-0002", NOW - timedelta(days=1), causes=())
    triaged(world, "P-0003", NOW - timedelta(days=2))
    triaged(world, "P-0004", NOW - timedelta(days=100))
    triaged(world, "P-0005", NOW - timedelta(days=1), causes=(RootCause("src/Other.src", 1),))
    open_issue = save_issue(world.conn, "0007", status=IssueStatus.NEEDS_DECISION, problems=("P-0005",))
    issues.save(world.conn, IssueRecord(replace(open_issue, root_cause=("src/Services/OrderService.src:30",)),
                                        "issues/0007-order-500.md", "0" * 64))
    save_issue(world.conn, "0008", problems=("P-0002",))
    found = dedup.candidates(world.conn, problem, FILES, NOW, 90, 10)
    assert [(item.id, item.kind) for item in found] == [("0007", "issue"), ("P-0002", "problem"), ("P-0003", "problem")]
    assert found[2].root_causes == ("src/Services/OrderService.src:12",)
    assert [item.id for item in dedup.candidates(world.conn, problem, FILES, NOW, 90, 1)] == ["0007"]


def test_dedup_output_is_checked_and_an_issue_target_becomes_its_first_problem(tmp_path):
    world = make_triage_world(tmp_path)
    problem = store_problem(world, "P-0003")
    save_issue(world.conn, "0007", status=IssueStatus.NEEDS_DECISION, problems=("P-0001",))
    found = [dedup.Candidate("0007", "issue", "订单查询返回 500", ("src/Services/OrderService.src:12",), "待审阅")]
    same = {"sameRootCause": True, "target": "0007", "evidence": ["src/Services/OrderService.src:12"], "reason": "同一处"}
    wrong = {**same, "target": "P-0009", "evidence": ["src/Services/OrderService.src:99"]}
    assert len(dedup.check(wrong, found, world.worktree)) == 2
    claim = claims.build(problem, [make_signal()], [])
    runner = FakeRunner({"triage-dedup": [wrong, same]})
    outcome = dedup.decide(calls(world, runner), world.conn, problem, claim, found)
    assert (outcome.target, outcome.statuses, outcome.note) == ("P-0001", [RunnerStatus.OK, RunnerStatus.OK], None)
    assert "target P-0009 不在候选中" in runner.tasks[1].instructions.prompt
    assert runner.tasks[0].capability == "light" and "- 0007(issue，待审阅)" in runner.tasks[0].instructions.prompt
    different = FakeRunner({"triage-dedup": [{"sameRootCause": False, "target": None, "evidence": [], "reason": "x"}]})
    assert dedup.decide(calls(world, different), world.conn, problem, claim, found).target is None
    failing = FakeRunner({"triage-dedup": [RunnerStatus.FAILED]})
    note = dedup.decide(calls(world, failing), world.conn, problem, claim, found).note
    assert note.startswith("查重没有得到合格的判断")
    assert dedup.decide(calls(world, failing), world.conn, problem, claim, []).statuses == []


class FakeGit:
    def __init__(self, commits):
        self.commits = commits

    def blame(self, repo, path, line_start, line_end, rev="HEAD"):
        if path not in self.commits:
            raise GitCommandError(f"no such path {path}")
        return [BlameLine(line_start, self.commits[path], "zhang", "z@example.test", NOW, "x")]


class FakePrs:
    def pr_for_commit(self, repo, commit):
        return PullState(185, "https://example.test/pull/185", "MERGED") if commit.startswith("a") else None


def test_attribution_lists_each_introducing_commit_and_explains_failures(tmp_path):
    git = FakeGit({"src/A.src": "a" * 40, "src/B.src": "b" * 40})
    causes = [RootCause("src/A.src", 3), RootCause("src/B.src", 4), RootCause("src/A.src", 9),
              RootCause("src/C.src", 1)]
    found = attribution.attribute(git, FakePrs(), tmp_path, tmp_path, "c" * 40, causes)
    assert [(item.commit[0], item.author, item.pr) for item in found.introduced_by] == [("a", "zhang", 185),
                                                                                       ("b", "zhang", None)]
    assert found.note == "src/C.src:1 的 git blame 失败：no such path src/C.src"
    assert attribution.attribute(git, None, tmp_path, tmp_path, "c" * 40, causes[:1]).introduced_by[0].pr is None


def test_tradeoffs_are_checked_against_the_knowledge_base(tmp_path):
    world = make_triage_world(tmp_path)
    world.knowledge("TO-0001", "export", "导出接口允许全量")
    world.knowledge("DP-0001", "owner", "列表查询按公司过滤")
    assert tradeoff_valid(world.conn, "TO-0001")
    assert not tradeoff_valid(world.conn, "DP-0001")
    assert not tradeoff_valid(world.conn, "TO-0009")
    assert not tradeoff_valid(world.conn, None)


def test_assessment_is_required_for_confirmed_and_checked(tmp_path):
    world = make_triage_world(tmp_path)
    missing = verification(assessment=assessment(estimate={"files": [{"path": "src/Missing.src", "isNew": False},
                                                                     {"path": "src/New.src", "isNew": True}],
                                                           "lines": 10}))
    assert evidence_checks.assessment_problems(missing, world.worktree) == [
        "预估改动的文件 src/Missing.src 在当前代码中不存在，新建的文件要标明 isNew"]
    deferred = verification(assessment=assessment("defer", reevaluateWhen=None))
    assert evidence_checks.assessment_problems(deferred, world.worktree) == [
        "建议暂不修时必须写明重估条件 assessment.reevaluateWhen"]
    assert evidence_checks.assessment_problems(verification(assessment=None), world.worktree)[0].startswith(
        "判为成立或条件成立时必须给出 assessment")
    assert evidence_checks.assessment_problems(verification("refuted"), world.worktree) == []


def test_rating_uses_report_impact_and_the_estimate(tmp_path):
    config = core_config()
    problem = make_problem()
    claim = claims.build(problem, [make_signal()], [])
    outputs = evidence_checks.outputs_for(claim, verification())
    found = rating.rate(config, problem, make_signal(), outputs, p0=False)
    assert (found.severity, found.complexity, found.tier, found.task_type, found.estimated_files) == (
        Severity.P2, Complexity.HIGH, SizeTier.MICRO, TaskType.BUG, FILES)
    many = evidence_checks.outputs_for(claim, verification(assessment=assessment(estimate={
        "files": [{"path": f"src/F{n}.src", "isNew": True} for n in range(4)], "lines": 120})))
    assert rating.rate(config, problem, make_signal(), many, p0=False).tier is SizeTier.MEDIUM
    server = make_problem(probe=Probe.PLATFORM_ERRORS)
    framed = make_signal(probe=Probe.PLATFORM_ERRORS, context={"projectFrames": [{"file": "src/A.src", "line": 1}]})
    assert rating.rate(config, server, framed, outputs, p0=False).complexity is Complexity.LOW
    refuted = evidence_checks.outputs_for(claim, verification("refuted"))
    found = rating.rate(config, problem, make_signal(), refuted, p0=True)
    assert (found.severity, found.tier, found.task_type) == (Severity.P0, None, None)
    assert rating.rate(config, problem, make_signal(), refuted, p0=False).severity is None
