from dataclasses import replace
from datetime import timedelta

import pytest
from pipeline_world import NOW, make_signal
from triage_world import make_triage_world, store_triaged, triage_outputs

from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import (
    CloseReason,
    IssuePhase,
    IssueStatus,
    ProblemEvent,
    ProblemStatus,
    Treatment,
    TriageOutcome,
)
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.pipeline.issue.steps.transitions import IssueCommandRejected
from tightrein.store.files import issue_files, suppressions
from tightrein.store.files.issue_files import IssueDocument
from tightrein.store.repos import issue_events, issues, problem_events, problems, triage


def service(world, clock=None):
    return IssueService(IssueDeps(world.layout, world.config, world.conn, clock or world.clock, world.events,
                                  snapshot=world.worktree))


def created(world, problem_id="P-0001", number=1, **outputs):
    store_triaged(world, problem_id, make_signal(number), outputs=triage_outputs(problem_id, **outputs))
    return service(world).create().items[0].issue_id


def text_of(world, issue_id):
    return (world.layout.root / issues.get(world.conn, issue_id).path).read_text(encoding="utf-8")


def test_approve_moves_to_todo_and_illegal_commands_name_the_allowed_ones(tmp_path):
    world = make_triage_world(tmp_path)
    issue_id = created(world)
    record = service(world).approve(issue_id, note="复现步骤可信")
    assert record.issue.status is IssueStatus.TODO
    assert "放行(操作者 user)：复现步骤可信" in text_of(world, issue_id)
    event = issue_events.for_issue(world.conn, issue_id)[-1]
    assert (event.event, event.from_status, event.to_status) == (
        "approve", IssueStatus.NEEDS_DECISION, IssueStatus.TODO)
    with pytest.raises(IssueCommandRejected, match="当前为「待修」，不能放行；可用的命令：close"):
        service(world).approve(issue_id)
    with pytest.raises(IssueCommandRejected, match="重新打开"):
        service(world).reopen(issue_id)


def test_wont_fix_ignores_the_problems_until_escalation_or_a_new_release(tmp_path):
    world = make_triage_world(tmp_path)
    issue_id = created(world)
    record = service(world).close(issue_id, CloseReason.WONT_FIX, note="内部角色，影响很小")
    assert (record.issue.status, record.issue.close_reason) == (IssueStatus.CANCELLED, CloseReason.WONT_FIX)
    problem = problems.get(world.conn, "P-0001")
    assert problem.status is ProblemStatus.IGNORED
    assert (problem.ignore_until.new_release, problem.ignore_until.severity_escalated) == (True, True)
    event = problem_events.for_problem(world.conn, "P-0001")[-1]
    assert (event.event, event.operation, event.detail["context"]["closeReason"]) == (
        ProblemEvent.ISSUE_CLOSED, "user_action", "wont-fix")
    reopened = service(world).reopen(issue_id, note="用户反馈变多")
    assert (reopened.issue.status, reopened.issue.close_reason) == (IssueStatus.TODO, None)


def test_not_a_bug_suppresses_the_problems_and_records_a_false_confirm(tmp_path):
    world = make_triage_world(tmp_path)
    issue_id = created(world)
    service(world).close(issue_id, CloseReason.NOT_A_BUG)
    assert problems.get(world.conn, "P-0001").status is ProblemStatus.IGNORED
    assert [rule.fingerprint for rule in suppressions.read(world.layout.suppressions())] == ["p-0001-fingerprint"]
    assert triage.latest(world.conn, "P-0001").result.outcome is TriageOutcome.FALSE_CONFIRM


def test_duplicate_moves_the_problems_to_the_other_issue(tmp_path):
    world = make_triage_world(tmp_path)
    first = created(world)
    second = created(world, "P-0002", 2, rootCauses=[{"file": "src/Controllers/OrderController.src", "line": 8,
                                                        "symbol": "OrderController.Get"}])
    with pytest.raises(IssueCommandRejected, match="--duplicate-of"):
        service(world).close(second, CloseReason.DUPLICATE)
    with pytest.raises(IssueCommandRejected, match="由 verify 与 release 写入"):
        service(world).close(second, CloseReason.FIXED)
    service(world).close(second, CloseReason.DUPLICATE, duplicate_of=first)
    target = issue_files.read(world.layout.root / issues.get(world.conn, first).path)
    assert target.issue.problems == ("P-0001", "P-0002")
    assert "Issue 0002 以重复关闭，并入问题 P-0002" in target.body
    assert problems.get(world.conn, "P-0002").issue_id == first


def test_sync_reopens_regressions_fills_outcomes_and_lists_overdue_reviews(tmp_path):
    world = make_triage_world(tmp_path)
    regressed = created(world)
    fixed = created(world, "P-0002", 2, rootCauses=[{"file": "src/Controllers/OrderController.src", "line": 8,
                                                      "symbol": "OrderController.Get"}])
    waiting = created(world, "P-0003", 3, rootCauses=[{"file": "src/Controllers/OrderController.src", "line": 9,
                                                        "symbol": "OrderController.List"}])
    for issue_id, reason in ((regressed, CloseReason.FIXED), (fixed, CloseReason.FIXED)):
        record = issues.get(world.conn, issue_id)
        document = issue_files.read(world.layout.root / record.path)
        issue_files.write(world.conn, world.layout, IssueDocument(
            replace(document.issue, status=IssueStatus.DONE, close_reason=reason), document.body))
    problems.save(world.conn, replace(problems.get(world.conn, "P-0001"), status=ProblemStatus.REGRESSED))
    later = FixedClock(NOW + timedelta(days=6))
    report = service(world, later).sync()
    assert (report.reopened, report.outcomes, report.overdue, report.invalid) == (
        [regressed], ["P-0002"], [waiting], None)
    assert issues.get(world.conn, regressed).issue.status is IssueStatus.TODO
    assert "关联问题 P-0001 回归" in text_of(world, regressed)
    assert triage.latest(world.conn, "P-0002").result.outcome is TriageOutcome.CORRECT


def test_sync_reindexes_hand_edited_files_and_reports_broken_ones(tmp_path):
    world = make_triage_world(tmp_path)
    issue_id = created(world)
    path = world.layout.root / issues.get(world.conn, issue_id).path
    path.write_text(path.read_text(encoding="utf-8").replace("title: 订单查询返回 500", "title: 订单查询报错"),
                    encoding="utf-8")
    assert service(world).sync().reindexed == [issue_id]
    assert issues.get(world.conn, issue_id).issue.title == "订单查询报错"
    path.write_text("没有 frontmatter", encoding="utf-8")
    assert "Issue 文件不合格" in service(world).sync().invalid


def test_review_reminders_skip_non_working_days(tmp_path):
    schedule = {"tick": {"weekdays": [1, 2, 3, 4, 5], "minutes": [0]}, "nonWorkingDays": ["2026-10-06", "2026-10-07"]}
    world = make_triage_world(tmp_path, schedule=schedule)
    created(world)
    assert service(world, FixedClock(NOW + timedelta(days=6))).sync().overdue == []


def test_edit_validates_records_the_changed_sections_and_restores_on_abandon(tmp_path):
    world = make_triage_world(tmp_path)
    issue_id = created(world)
    path = world.layout.root / issues.get(world.conn, issue_id).path
    original = path.read_text(encoding="utf-8")

    def rewrite(old, new):
        return lambda target: target.write_text(target.read_text(encoding="utf-8").replace(old, new),
                                                encoding="utf-8")

    asked = []
    result = service(world).edit(issue_id, rewrite("status: needs-decision", "status: todo"),
                                 lambda errors: asked.append(errors) or False)
    assert not result.saved and path.read_text(encoding="utf-8") == original
    assert asked == [["status 与 closeReason 只能用 tightrein approve、close、reopen 修改"]]
    missing = service(world).edit(issue_id, rewrite("### 复现", "### 重现"), lambda errors: False)
    assert not missing.saved and path.read_text(encoding="utf-8") == original
    saved = service(world).edit(issue_id, rewrite("在 OrderService.Get 中按公司过滤", "在查询入口按公司过滤"),
                                lambda errors: False)
    assert (saved.saved, saved.sections) == (True, ("修复方向",))
    assert "用户编辑：修复方向" in path.read_text(encoding="utf-8")
    assert issue_events.for_issue(world.conn, issue_id)[-1].event == "user-edited"
    assert issues.get(world.conn, issue_id).file_sha256 == issue_files.content_hash(path.read_text(encoding="utf-8"))


def test_list_orders_by_treatment_then_severity(tmp_path):
    world = make_triage_world(tmp_path)
    low = created(world, treatment="scheduled", severity="P3")
    high = created(world, "P-0002", 2, treatment="scheduled", severity="P1", rootCauses=[
        {"file": "src/Controllers/OrderController.src", "line": 8, "symbol": "OrderController.Get"}])
    urgent = created(world, "P-0003", 3, treatment="immediate", severity="P2", rootCauses=[
        {"file": "src/Controllers/OrderController.src", "line": 9, "symbol": "OrderController.List"}])
    assert [record.issue.id for record in service(world).list()] == [urgent, high, low]
    assert issues.get(world.conn, urgent).issue.treatment is Treatment.IMMEDIATE
    view = service(world).show(low)
    assert [problem.id for problem in view.problems] == ["P-0001"] and "### 问题" in view.body


def test_not_reproduced_requests_a_retriage_and_updates_are_written_with_the_status(tmp_path):
    from tightrein.domain.enums import IssueEvent
    from tightrein.pipeline.issue.steps import transitions
    from tightrein.pipeline.triage.steps import select

    world = make_triage_world(tmp_path)
    issue_id = created(world)
    env = transitions.IssueEnv(world.conn, world.layout, world.clock, world.config)
    service(world).approve(issue_id)
    started = transitions.apply_event(env, issues.get(world.conn, issue_id), IssueEvent.FIX_STARTED, actor="fix",
                                      updates={"branch": "cty/fix-order-500"})
    assert (started.issue.status, started.issue.phase, started.issue.branch) == (
        IssueStatus.IN_PROGRESS, IssuePhase.FIX, "cty/fix-order-500")
    with pytest.raises(ValueError, match="只能一并写入 branch、pr：status"):
        transitions.apply_event(env, started, IssueEvent.FIX_DONE, updates={"status": IssueStatus.DONE})
    back = transitions.apply_event(env, started, IssueEvent.NOT_REPRODUCED, actor="verify", note="基准 commit 上返回 200")
    assert (back.issue.status, back.issue.phase) == (IssueStatus.NEEDS_DECISION, None)
    event = problem_events.for_problem(world.conn, "P-0001")[-1]
    assert event.event is ProblemEvent.RETRIAGE_REQUESTED
    assert event.reason == f"Issue {issue_id} 修复前复现不了，需要重新分诊：基准 commit 上返回 200"
    assert select.retriage_requests(world.conn, "P-0001") == [event.id]
    assert triage.latest(world.conn, "P-0001").result.outcome is TriageOutcome.FALSE_CONFIRM


def test_annotations_write_history_and_fields_without_a_status_change(tmp_path):
    from tightrein.domain.enums import Stage
    from tightrein.domain.issue import Hold
    from tightrein.pipeline.issue.steps import transitions

    world = make_triage_world(tmp_path)
    issue_id = created(world)
    env = transitions.IssueEnv(world.conn, world.layout, world.clock, world.config)
    hold = Hold("准备命令失败", Stage.FIX, NOW, "npm ci 退出码 1")
    record = transitions.annotate(env, issue_id, "建立修复分支 cty/fix-order-500", updates={"branch": "cty/fix-order-500",
                                                                                        "hold": hold})
    assert (record.issue.status, record.issue.branch, record.issue.hold) == (IssueStatus.NEEDS_DECISION,
                                                                            "cty/fix-order-500", hold)
    assert "建立修复分支 cty/fix-order-500" in text_of(world, issue_id)
    assert [event.event for event in issue_events.for_issue(world.conn, issue_id)] == []
    with pytest.raises(ValueError, match="只能一并写入"):
        transitions.annotate(env, issue_id, "x", updates={"status": IssueStatus.TODO})
