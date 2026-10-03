from datetime import timedelta, timezone

from pipeline_world import NOW, make_signal
from triage_world import COMMIT, FakeRunner, assessment, make_triage_world, store_problem, verification

from tightrein.domain.clock import local_date
from tightrein.domain.enums import (
    Disposition,
    HandoffStatus,
    ProblemStatus,
    RunStatus,
    Severity,
    Stage,
    SizeTier,
    TaskType,
    Treatment,
    TriageOutcome,
    Verdict,
)
from tightrein.domain.triage import RootCause, TriageResult
from tightrein.pipeline.triage.render import findings
from tightrein.pipeline.triage.service import TriageDeps, TriageRequest, TriageService
from tightrein.store import locks
from tightrein.store.files import handoff_files, markdown, suppressions
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import budget_usage, problems, runs, scores, triage
from tightrein.store.repos.triage import TriageRecord
from tightrein.vcs.errors import GitCommandError
from tightrein.vcs.parse import BlameLine

TOKYO = timezone(timedelta(hours=9), "JST")
DEDUP_SAME = {"sameRootCause": True, "target": "P-0002", "evidence": ["src/Services/OrderService.src:12"],
              "reason": "同一处缺少过滤"}


class FakeGit:
    def log(self, repo, rev_range, paths=None, limit=None):
        return []

    def blame(self, repo, path, line_start, line_end, rev="HEAD"):
        return [BlameLine(line_start, "a" * 40, "zhang", "z@example.test", NOW, "x")]


def service(world, runner, layout=None, sync=None):
    return TriageService(TriageDeps(layout or world.layout, world.tool, world.config, world.conn, world.clock,
                                    world.events, runner, FakeGit(), sync or (lambda commit: COMMIT), zone=TOKYO))


def outputs_of(item):
    return handoff_files.read(item.handoff)


def test_a_confirmed_problem_goes_to_issue_creation_with_a_finding_report(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    runner = FakeRunner({"claim-verifier": [verification()]})
    result = service(world, runner).run(TriageRequest())
    item = result.items[0]
    assert (item.status, item.verdict, item.severity, item.disposition) == (
        HandoffStatus.OK, Verdict.CONFIRMED, Severity.P2, Disposition.CREATE_ISSUE)
    assert runner.roles() == ["claim-verifier"]
    document = outputs_of(item)
    outputs = document["outputs"]
    assert (document["nextAction"], outputs["treatment"], outputs["taskType"], outputs["sizeTier"],
            outputs["introducedBy"][0]["author"]) == ("交给 issue 创建", "scheduled", "bug", "micro", "zhang")
    assert outputs["worth"]["direction"] == "在 OrderService.Get 中按公司过滤"
    assert outputs["claim"]["facts"][-1]["label"] == "main 差异"
    assert outputs["attempts"] == [{"role": "claim-verifier", "statuses": ["ok"]}]
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.ONGOING
    record = triage.latest(world.conn, "P-0001")
    assert (record.result.attempt, record.result.root_causes[0].symbol) == (1, "OrderService.Get")
    assert (record.result.treatment, record.result.task_type, record.result.size_tier) == (
        Treatment.SCHEDULED, TaskType.BUG, SizeTier.MICRO)
    assert len(scores.find(world.conn, run_id=result.run.id)) == 5
    report = markdown.read(world.layout.finding("P-0001"))
    assert (report.frontmatter["id"], report.frontmatter["status"]) == ("triage-P-0001", "create-issue")
    assert "path:src/Services/OrderService.src" in report.frontmatter["tags"]
    headings = [line[3:] for line in report.body.splitlines() if line.startswith("## ")]
    assert headings == list(findings.SECTIONS)
    assert "分诊时间：2026-10-05 12:00(JST)" in report.body
    assert result.run.status is RunStatus.OK and world.layout.run_report(result.run.id).is_file()


def test_refuted_problems_become_false_positives_with_a_suppression(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    runner = FakeRunner({"claim-verifier": [verification("refuted")]})
    item = service(world, runner).run(TriageRequest()).items[0]
    assert item.disposition is Disposition.FALSE_POSITIVE and runner.roles() == ["claim-verifier"]
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.IGNORED
    assert [rule.fingerprint for rule in suppressions.read(world.layout.suppressions())] == ["p-0001-fingerprint"]


def test_a_refuted_p0_is_refuted_again_and_disagreement_goes_to_the_manual_queue(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, signal=make_signal(check="unauthorized_role_access", context={"response": {"status": 200}}))
    runner = FakeRunner({"claim-verifier": [verification("refuted")], "refuter": [verification()]})
    item = service(world, runner).run(TriageRequest()).items[0]
    assert runner.roles() == ["claim-verifier", "refuter"]
    assert (item.status, item.disposition, item.verdict) == (HandoffStatus.BLOCKED, Disposition.MANUAL_QUEUE,
                                                             Verdict.CONFIRMED)
    document = outputs_of(item)
    assert document["outputs"]["refuterVerdict"] == "confirmed"
    assert "与取证的不成立不一致" in document["blockedReason"]
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.NEW


def test_evidence_that_keeps_failing_the_checks_goes_to_the_manual_queue(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    runner = FakeRunner({"claim-verifier": [verification(trigger="可能在并发时出现")]})
    item = service(world, runner).run(TriageRequest()).items[0]
    assert len(runner.tasks) == 3 and item.disposition is Disposition.MANUAL_QUEUE
    assert item.verdict is Verdict.INSUFFICIENT and "[triage.no-vague-wording]" in item.reason
    queue = service(world, runner).queue()
    assert [(entry.problem_id, entry.findings) for entry in queue] == [("P-0001", "data/findings/P-0001.md")]


def test_fixed_on_main_waits_for_the_deployment(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    fixed = verification(fixedOnMain={"commit": "d" * 40, "basis": "该提交加上了公司过滤"})
    runner = FakeRunner({"claim-verifier": [fixed]})
    item = service(world, runner).run(TriageRequest()).items[0]
    assert item.disposition is Disposition.AWAITING_DEPLOY and runner.roles() == ["claim-verifier"]


def test_dedup_merges_into_a_recent_problem_at_the_same_place(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0002", make_signal(2), status=ProblemStatus.ONGOING)
    triage.save(world.conn, TriageRecord(TriageResult("P-0002", 1, Verdict.CONFIRMED, Disposition.CREATE_ISSUE,
                                                      "入口没有校验", COMMIT,
                                                      root_causes=(RootCause("src/Services/OrderService.src", 12),)),
                                         "R-20261004-030000-triage", NOW - timedelta(days=1)))
    store_problem(world, "P-0003", make_signal(3))
    runner = FakeRunner({"triage-dedup": [DEDUP_SAME]})
    item = service(world, runner).run(TriageRequest()).items[0]
    assert (item.problem_id, item.merged_into, item.findings) == ("P-0003", "P-0002", None)
    assert outputs_of(item)["nextAction"] == "已并入 P-0002"
    assert problems.get(world.conn, "P-0003").merged_into == "P-0002"
    assert triage.latest(world.conn, "P-0003") is None


def test_high_risk_conclusions_are_refuted_and_disagreement_goes_to_the_manual_queue(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    security = verification(assessment=assessment(taskType="security"))
    runner = FakeRunner({"claim-verifier": [security], "refuter": [verification("refuted")]})
    item = service(world, runner).run(TriageRequest()).items[0]
    assert runner.roles() == ["claim-verifier", "refuter"]
    assert (item.disposition, item.verdict) == (Disposition.MANUAL_QUEUE, Verdict.CONFIRMED)
    agreed = FakeRunner({"claim-verifier": [verification(report=dict(REPORT, severity="P1"))],
                         "refuter": [verification()]})
    other = make_triage_world(tmp_path / "other")
    store_problem(other)
    found = service(other, agreed).run(TriageRequest()).items[0]
    assert agreed.roles() == ["claim-verifier", "refuter"]
    assert (found.disposition, outputs_of(found)["outputs"]["treatment"]) == (Disposition.CREATE_ISSUE, "immediate")


REPORT = {"title": "[订单] 其他公司的订单查询返回 500", "summary": "查询失败。", "steps": ["查询"], "expected": "403",
          "actual": "500", "acceptance": ["返回 403"], "severity": "P2", "severityReason": "非核心"}


def test_retriage_with_a_note_and_a_user_override(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    runner = FakeRunner({"claim-verifier": [verification("refuted")]})
    service(world, runner).run(TriageRequest())
    assert service(world, runner).run(TriageRequest(select=("P-0001",))).skipped == [
        ("P-0001", "P-0001 状态为 已忽略，只能分诊新发现、持续或回归的问题；已忽略的问题先执行 tightrein problem reopen")]
    run = service(world, runner).override("P-0001", Verdict.CONFIRMED, "抽查发现确实越权")
    item = run.items[0]
    assert (item.disposition, item.verdict) == (Disposition.CREATE_ISSUE, Verdict.CONFIRMED)
    records = triage.for_problem(world.conn, "P-0001")
    assert [(record.result.attempt, record.result.outcome) for record in records] == [
        (1, TriageOutcome.FALSE_REFUTE), (2, TriageOutcome.OVERRIDDEN)]
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.ONGOING
    assert outputs_of(item)["outputs"]["evidence"]["sourceOfPhenomenon"]["explanation"] == "入口已拒绝非法编号"
    again = FakeRunner({"claim-verifier": [verification()]})
    retried = service(world, again).retriage("P-0001", note="只在月底结账时出现")
    assert retried.items[0].disposition is Disposition.CREATE_ISSUE
    assert "- 只在月底结账时出现(用户提供)" in again.tasks[0].instructions.prompt
    assert outputs_of(retried.items[0])["outputs"]["claim"]["userNotes"] == ["只在月底结账时出现"]


def test_budget_locks_and_worktree_failures_stop_or_skip(tmp_path):
    world = make_triage_world(tmp_path, stages={"triage": {"budgetPerDay": 1}})
    store_problem(world)
    store_problem(world, "P-0002", make_signal(2))
    locks.acquire(world.conn, "P-0001", world.clock, timedelta(hours=1), holder=locks.Holder(1, "other-host"))
    runner = FakeRunner({"claim-verifier": [verification("refuted")]})
    result = service(world, runner).run(TriageRequest())
    assert result.skipped == [("P-0001", "正被其他运行处理")]
    assert [item.problem_id for item in result.items] == ["P-0002"]
    budget_usage.add(world.conn, Stage.TRIAGE, local_date(NOW), 1.5, 10, 10, False, world.clock)
    store_problem(world, "P-0003", make_signal(3))
    blocked = service(world, runner).run(TriageRequest(select=("P-0003",)))
    assert blocked.skipped == [("P-0003", "当天 triage 的费用已达到 stages.triage.budgetPerDay，留到下一次")]

    def broken(commit):
        raise GitCommandError("worktree 不干净")

    failed = service(world, runner, sync=broken).run(TriageRequest(select=("P-0003",)))
    assert failed.run is None and failed.message.endswith("先执行 tightrein worktree sync")


def test_output_mode_writes_only_to_the_output_directory_and_dry_run_calls_nothing(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    output = tmp_path / "sandbox"
    runner = FakeRunner({"claim-verifier": [verification("refuted")]})
    sandbox = WorkspaceLayout(world.layout.root, output)
    item = service(world, runner, layout=sandbox).run(TriageRequest()).items[0]
    assert item.handoff.parent == output / "handoff" and item.findings == output / "findings" / "P-0001.md"
    assert triage.latest(world.conn, "P-0001") is None and runs.find(world.conn) != []
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.NEW
    assert not world.layout.suppressions().exists()
    planned = service(world, FakeRunner()).run(TriageRequest(dry_run=True))
    assert [(entry.problem_id, entry.role) for entry in planned.plan] == [("P-0001", "claim-verifier")]


def test_a_program_error_fails_only_that_problem(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world)
    store_problem(world, "P-0002", make_signal(2))
    runner = FakeRunner({"claim-verifier": [{"verdict": "confirmed"}, verification("refuted")]})
    result = service(world, runner).run(TriageRequest())
    statuses = sorted((item.problem_id, item.status) for item in result.items)
    assert statuses == [("P-0001", HandoffStatus.FAILED), ("P-0002", HandoffStatus.OK)]
    failed = next(item for item in result.items if item.status is HandoffStatus.FAILED)
    assert failed.reason.startswith("KeyError") and result.exit_code == 1
    assert result.run.status is RunStatus.PARTIAL
