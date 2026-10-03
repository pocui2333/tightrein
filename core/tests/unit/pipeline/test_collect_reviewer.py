import pytest
from pipeline_world import RELEASE
from triage_world import FakeRunner, make_triage_world, verification

from tightrein.domain.enums import ProbeLevel, RunnerStatus, Stage, ViolationKind
from tightrein.guards.report import Violation
from tightrein.packaging import third_party
from tightrein.pipeline.collect.prompts.static_reviewer import StaticReviewer
from tightrein.runner.roles import READ_ONLY_COMMANDS, installed_skills
from tightrein.sources.static.baseline import Batch, SourceFile
from tightrein.sources.static.reviewer import Claim, ToolFinding
from tightrein.sources.static.scope import Scope
from tightrein.retrieval.context import ContextBundle
from tightrein.runner.result import DAILY_BUDGET, RunnerResult
from tightrein.runner.task import ContextItem, Limits, SkillRef
from tightrein.store.files.layout import ToolLayout

RUN_ID = "R-20261005-030000-collect-static"
SCOPE = Scope(ProbeLevel.INCREMENTAL, "b" * 40, RELEASE, ("src/Services/OrderService.src",),
              ("src/Services/OrderService.src",))
CLAIM = {"file": "src/Services/OrderService.src", "line": 12, "ruleOrPattern": "DP-0001", "layer": "incremental",
         "statement": "查询没有按公司过滤", "trigger": "传入其他公司的订单编号"}
FINDING = ToolFinding("semgrep", "rule", "no-filter", "src/Services/OrderService.src", 12, None, "缺少过滤", "WARNING")


def reviewer(world, runner, context=None):
    return StaticReviewer(runner=runner, tool=world.tool, layout=world.layout, config=world.config, conn=world.conn,
                          clock=world.clock, run_id=RUN_ID, workdir=world.worktree, context=context)


def test_review_uses_the_role_file_changed_files_findings_and_prefetched_knowledge(tmp_path):
    world = make_triage_world(tmp_path)
    runner = FakeRunner({"static-review": [{"claims": [CLAIM], "excluded": []}]})
    requests = []

    def context(request):
        requests.append(request)
        return ContextBundle([ContextItem("DP-0001", "defect-pattern", "列表查询按公司过滤", "knowledge/x.md", "path")])

    result = reviewer(world, runner, context).review(SCOPE, [FINDING])
    assert (result.status, result.claims) == (RunnerStatus.OK, (Claim.from_dict(CLAIM),))
    assert result.transcript == f"transcripts/static-review-{RUN_ID}.jsonl"
    task = runner.tasks[0]
    assert (task.role, task.subject.type, task.subject_id, task.stage, task.workdir) == (
        "static-review", "run", RUN_ID, Stage.COLLECT, world.worktree)
    assert (task.capability, task.limits, task.allowed_commands) == (
        "strong", Limits(max_turns=60, max_duration_ms=1200000), READ_ONLY_COMMANDS)
    prompt = task.instructions.prompt
    assert prompt.startswith("# static-review：静态巡检的增量审查")
    assert "- src/Services/OrderService.src" in prompt and '"rule": "no-filter"' in prompt
    assert "DP-0001 [defect-pattern] 列表查询按公司过滤" in prompt
    assert requests[0].paths == ("src/Services/OrderService.src",)
    assert task.instructions.skills == ()


def test_baseline_review_lists_the_batch_files_and_uses_the_strong_tier(tmp_path):
    world = make_triage_world(tmp_path)
    baseline_claim = {**CLAIM, "layer": "baseline", "severity": "high"}
    runner = FakeRunner({"baseline-review": [{"claims": [baseline_claim], "excluded": []}]})
    requests = []

    def context(request):
        requests.append(request)
        return ContextBundle([])

    batch = Batch(2, 3, "src/Services", (SourceFile("src/Services/OrderService.src", 120),))
    result = reviewer(world, runner, context).review_baseline(batch, [FINDING])
    assert result.claims == (Claim.from_dict(baseline_claim),) and result.claims[0].severity == "high"
    assert (result.cost_usd, result.exhausted) == (0.01, False)
    task = runner.tasks[0]
    assert (task.role, task.capability, task.output_schema) == (
        "baseline-review-2", "strong", "runner/roles/static-review.schema.json")
    assert task.limits == Limits(max_turns=80, max_duration_ms=1800000)
    prompt = task.instructions.prompt
    assert prompt.startswith("# baseline-review：静态巡检的基线审查")
    assert "第 2/3 批(src/Services，1 个文件、120 行)" in prompt and "- `src/Services/OrderService.src`(120 行)" in prompt
    assert '"rule": "no-filter"' in prompt and requests[0].paths == ("src/Services/OrderService.src",)


def test_baseline_review_reports_an_exhausted_daily_budget(tmp_path):
    world = make_triage_world(tmp_path)

    class BudgetRunner(FakeRunner):
        def run(self, task, *, clock, runner_override=None, model_override=None):
            self.tasks.append(task)
            return RunnerResult(RunnerStatus.LIMIT_REACHED, "fake", error_type=DAILY_BUDGET, attempts=0)

    batch = Batch(1, 1, "src", (SourceFile("src/a.src", 3),))
    result = reviewer(world, BudgetRunner()).review_baseline(batch, [])
    assert (result.status, result.detail, result.exhausted) == (RunnerStatus.LIMIT_REACHED, DAILY_BUDGET, True)


def test_variant_scan_reads_the_pattern_and_tradeoffs(tmp_path):
    world = make_triage_world(tmp_path)
    world.knowledge("DP-0001", "owner", "列表查询按公司过滤", body="检出方法：查找没有公司条件的查询。\n")
    world.knowledge("TO-0001", "export", "导出接口允许全量", tags=("export",))
    runner = FakeRunner({"variant-scan": [{"claims": [], "excluded": []}]})
    review = reviewer(world, runner)
    assert review.defect_patterns() == ("DP-0001",)
    result = review.scan_variants("DP-0001", SCOPE)
    assert result.status is RunnerStatus.OK and result.claims == ()
    task = runner.tasks[0]
    assert task.role == "variant-scan-dp-0001" and task.output_schema == "runner/roles/static-review.schema.json"
    assert "检出方法：查找没有公司条件的查询。" in task.instructions.prompt
    assert "- TO-0001 导出接口允许全量" in task.instructions.prompt


def test_verify_gives_only_the_claim_and_numbers_each_call(tmp_path):
    world = make_triage_world(tmp_path)
    runner = FakeRunner({"claim-verifier": [verification(), RunnerStatus.LIMIT_REACHED]})
    review = reviewer(world, runner)
    first = review.verify(Claim.from_dict(CLAIM))
    second = review.verify(Claim.from_dict(CLAIM))
    assert (first.status, first.verdict, first.completed_at) == (RunnerStatus.OK, verification()["verdict"],
                                                                 world.clock.now())
    assert (second.status, second.output, second.detail) == (RunnerStatus.LIMIT_REACHED, None, "fake-error")
    assert [task.role for task in runner.tasks] == ["claim-verifier-1", "claim-verifier-2"]
    task = runner.tasks[0]
    assert (task.capability, task.output_schema) == ("strong", "runner/roles/claim-verifier.schema.json")
    assert task.instructions.prompt.startswith("# claim-verifier：核实一条主张在代码里是否成立")
    assert "# 取证底线" in task.instructions.prompt and "查询没有按公司过滤" in task.instructions.prompt


def test_failed_reviews_carry_the_status_without_claims(tmp_path):
    world = make_triage_world(tmp_path)
    result = reviewer(world, FakeRunner({"static-review": [RunnerStatus.GUARD_VIOLATION]})).review(SCOPE, [])
    assert (result.status, result.claims, result.detail) == (RunnerStatus.GUARD_VIOLATION, (), "fake-error")
    hidden = Violation(ViolationKind.HIDDEN_PATH_READ, "regressions/x", "读取了隐藏路径")
    blocked = RunnerResult(RunnerStatus.GUARD_VIOLATION, "fake", attempts=1, violations=(hidden, hidden))
    verified = reviewer(world, FakeRunner({"claim-verifier": [blocked]})).verify(Claim.from_dict(CLAIM))
    assert (verified.status, verified.violations) == (RunnerStatus.GUARD_VIOLATION, ("hidden-path-read",))


def test_locked_third_party_skills_are_verified_before_they_are_referenced(tmp_path):
    tool = ToolLayout(tmp_path / "tool")
    skill = tool.skill("differential-review")
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: differential-review\ndescription: x\n---\n", encoding="utf-8")
    tool.third_party_lock().parent.mkdir(parents=True)
    files = third_party.file_hashes(skill.parent)
    locked = third_party.LockedSkill("differential-review", "https://github.com/example/skills", ref="0" * 40,
                                     path="skills/differential-review", tree_hash=third_party.tree_hash(files),
                                     files=files, locked_at="2026-10-05T03:00:00Z",
                                     verification=third_party.Verification("2026-10-05T03:00:00Z", 6000,
                                                                           "2026-10-01", False))
    third_party.write_lock(tool.third_party_lock(), [locked])
    assert installed_skills(tool, ("differential-review", "variant-analysis")) == (SkillRef("differential-review"),)
    skill.write_text("被改过\n", encoding="utf-8")
    with pytest.raises(third_party.HashMismatch, match="SKILL.md"):
        installed_skills(tool, ("differential-review",))
