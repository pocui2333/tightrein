import subprocess
from dataclasses import replace
from datetime import timedelta, timezone

from pipeline_world import make_signal, make_world, save_issue
from triage_world import make_triage_world, store_triaged

from tightrein.contracts import validate
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import HandoffStatus, IssueOrigin, IssuePhase, IssueStatus, RunStage, RunStatus, Treatment
from tightrein.observability.notify import Notifier
from tightrein.observability.redact import Redactor
from tightrein.orchestrator import inbox, runlog, summary
from tightrein.orchestrator.rules import StepOutcome
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.store.files import documents, handoff_files
from tightrein.store.repos import issues, triage
from tightrein.store.repos.issues import IssueRecord

STEPS = [StepOutcome(1, "recovery", True, "没有中断的运行", [], RunStatus.OK, 0),
         StepOutcome(2, "triage", False, "触发条件不满足"),
         StepOutcome(3, "learn-lessons", True, "总是执行", ["R-20261005-030000-learn"], RunStatus.FAILED, 12)]

TTL = timedelta(minutes=120)


class Commands:
    def __init__(self, code=0):
        self.code = code
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(list(args), self.code, "", "boom" if self.code else "")


def write(world, values):
    loop = runlog.begin(world.conn, world.clock, world.events, TTL)
    world.clock.advance(timedelta(seconds=1))
    return summary.write(world.layout, world.conn, world.clock, loop, values, language="zh", zone=timezone.utc)


def notifier(world, commands):
    return Notifier(world.conn, "macos", world.clock, Redactor(), run=commands, zone=timezone.utc)


def test_waiting_items_pin_immediate_fixes(tmp_path):
    world = make_triage_world(tmp_path)
    store_triaged(world, "P-0001", make_signal(1))
    record = triage.latest(world.conn, "P-0001")
    triage.save(world.conn, replace(record, result=replace(record.result, treatment=Treatment.IMMEDIATE)))
    save_issue(world.conn, "0003", IssueStatus.PENDING_MERGE)
    save_issue(world.conn, "0007", IssueStatus.NEEDS_DECISION, problems=("P-0001",))
    items = inbox.items(world.conn)
    assert [(item["kind"], item["subjectId"]) for item in items] == [("issue-approval", "0007"),
                                                                     ("pr-review", "0003")]
    assert items[0]["command"] == "tightrein issue approve 7"
    assert (items[0]["treatment"], items[0]["severity"]) == ("immediate", "P1")


def test_queued_follow_ups_are_not_waiting_items(tmp_path):
    world = make_triage_world(tmp_path)
    save_issue(world.conn, "0003", IssueStatus.IN_PROGRESS)
    queued = replace(save_issue(world.conn, "0004", IssueStatus.TODO), origin=IssueOrigin.MANUAL, depends_on="0003")
    issues.save(world.conn, IssueRecord(queued, "issues/0004-order-500.md", "0" * 64))
    assert [item["subjectId"] for item in inbox.items(world.conn)] == ["0003"]
    save_issue(world.conn, "0003", IssueStatus.DONE, phase=IssuePhase.DEPLOY_CHECK)
    assert [item["subjectId"] for item in inbox.items(world.conn)] == ["0004", "0003"]


def test_immediate_fixes_are_pinned_first(tmp_path):
    world = make_triage_world(tmp_path)
    for number, treatment in enumerate((Treatment.SCHEDULED, Treatment.IMMEDIATE), start=1):
        problem_id = f"P-000{number}"
        store_triaged(world, problem_id, make_signal(number))
        record = triage.latest(world.conn, problem_id)
        triage.save(world.conn, replace(record, result=replace(record.result, treatment=treatment)))
        save_issue(world.conn, f"000{number}", IssueStatus.NEEDS_DECISION, problems=(problem_id,))
    save_issue(world.conn, "0003", IssueStatus.PENDING_MERGE)
    items = inbox.items(world.conn)
    assert [item["subjectId"] for item in items] == ["0002", "0001", "0003"]


def test_outputs_fit_the_schema_and_render_every_section(tmp_path):
    world = make_world(tmp_path)
    save_issue(world.conn, "0007", IssueStatus.NEEDS_DECISION)
    anomalies = [{"source": "learn-lessons", "reason": "执行器不可用", "log": "data/logs/events-2026-10-05.jsonl"}]
    values = summary.outputs(world.conn, world.layout, world.config, STEPS, anomalies, [])
    assert values["conclusion"] == "有 1 项等待用户处理；1 个步骤失败：learn-lessons"
    assert values["waiting"][0]["recommendation"] == "核对问题与范围后放行" and "issue-approve" in values["waiting"][0]["reason"]
    assert validate.validate("handoff/outputs/loop.schema.json", values) == []
    handoff, report = write(world, values)
    assert handoff_files.read(handoff)["outputs"]["waiting"][0]["subjectId"] == "0007"
    assert report == world.layout.daily_report(world.clock.now().date())
    parsed = documents.read(report)
    assert parsed.header["kind"] == "progress" and parsed.header["status"] == "blocked"
    assert parsed.blocks["checklist"][0] == {"item": "[issue-approval] 0007 订单查询返回 500(待决定)", "state": "blocked",
                                            "owner": "user"}
    assert "learn-lessons：执行器不可用" in parsed.sections["blockers"]
    assert "**接入中的项目**" in parsed.sections["checklist"]
    _, again = write(world, values)
    assert documents.read(again).conclusion.startswith("待处理 1 项；今天运行 2 次")


def test_unsynced_github_mirrors_are_listed(tmp_path):
    world = make_world(tmp_path, issues={"tracker": "github"})
    save_issue(world.conn, "0007", IssueStatus.TODO)
    values = summary.outputs(world.conn, world.layout, world.config, STEPS, [], [])
    assert values["unsynced"] == ["0007：未建 GitHub Issue"]
    assert validate.validate("handoff/outputs/loop.schema.json", values) == []
    assert "未同步到 GitHub：0007：未建 GitHub Issue" in documents.read(write(world, values)[1]).sections["blockers"]
    assert summary.outputs(make_world(tmp_path / "local").conn, world.layout, world.config, STEPS, [], [])[
        "unsynced"] == []


def test_notifications_skip_quiet_scheduled_runs_and_report_failures(tmp_path):
    world = make_world(tmp_path)
    quiet = summary.outputs(world.conn, world.layout, world.config, STEPS[:1], [], [])
    commands = Commands()
    assert summary.notify(notifier(world, commands), "demo", "R-1", quiet, tmp_path / "r.md", [],
                          scheduled=True) is None
    health = [{"check": "missed-runs", "detail": "static 连续 2 次漏跑", "notify": True}]
    sent = summary.notify(notifier(world, commands), "demo", "R-20261005-030000-loop", quiet, tmp_path / "r.md",
                          health, scheduled=True)
    assert sent.status == "sent" and "static 连续 2 次漏跑" in commands.calls[0][2]
    assert "tightrein：demo" in commands.calls[0][2]
    failed = summary.notify(notifier(world, Commands(1)), "demo", "R-20261005-030001-loop", quiet,
                            tmp_path / "r.md", [], scheduled=False)
    assert summary.failed_notice(failed)["source"] == "notify"


def test_automatic_decisions_of_this_run_are_listed(tmp_path):
    world = make_triage_world(tmp_path, gates={"issue-approve": "auto", "plan-confirm": "auto", "fix-session": "auto"})
    store_triaged(world, "P-0001", make_signal(1))
    created = IssueService(IssueDeps(world.layout, world.config, world.conn, world.clock, world.events,
                                     snapshot=world.worktree)).create()
    run = stage_runs.begin(RunStage.RELEASE, world.layout, world.conn, world.clock, world.events)
    merge = {"merged": False, "reasons": ["有未通过或进行中的检查"], "operationId": None,
             "at": format_iso(world.clock.now())}
    run.handoff(RunStage.RELEASE, "0001", HandoffStatus.OK, {
        "issueId": "0001", "branch": "cty/fix", "commits": [], "syncs": [], "deployments": [],
        "pendingOperations": [], "autoMerge": merge}, "继续跟踪")
    values = summary.outputs(world.conn, world.layout, world.config, STEPS, [], [created.run.id, run.id])
    assert [(item["subjectId"], item["decision"]) for item in values["produced"]["autonomy"]] == [
        ("0001", "auto-approved"), ("0001", "merge-waiting")]
    assert validate.validate("handoff/outputs/loop.schema.json", values) == []
    assert "0001 未自动合并(有未通过或进行中的检查)" in documents.read(write(world, values)[1]).sections["completed"]


def test_reroutes_are_listed(tmp_path):
    world = make_world(tmp_path)
    line = "git push origin cty/fix-x：直连失败(Failed to connect to github.com)，改为经代理重试成功"
    values = summary.outputs(world.conn, world.layout, world.config, STEPS, [], [], [line])
    assert validate.validate("handoff/outputs/loop.schema.json", values) == []
    assert f"网络换路：{line}" in documents.read(write(world, values)[1]).sections["blockers"]
