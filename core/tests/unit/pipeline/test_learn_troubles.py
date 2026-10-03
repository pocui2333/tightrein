from datetime import timedelta

from learn_world import fix_outputs, issue, make_learn_world, problem_with, save_handoff, triaged
from pipeline_world import NOW

from tightrein.domain.enums import (
    CloseReason,
    Disposition,
    IssueEvent,
    IssueStatus,
    OperationExecutor,
    OperationKind,
    OperationStatus,
    RunStage,
    Stage,
    TriageOutcome,
    Verdict,
)
from tightrein.pipeline.learn.steps import troubles
from tightrein.store.repos import issue_events, pending_operations, pulls
from tightrein.store.repos.issue_events import IssueEventRecord
from tightrein.store.repos.pending_operations import PendingOperationRecord
from tightrein.store.repos.pulls import PullRecord

FIX_RUN = "R-20261001-030000-fix"


def kinds(found):
    return [(item.kind, item.keys) for item in found]


def operation(world, number, kind, status, subject="0007", result=None):
    pending_operations.save(world.conn, PendingOperationRecord(
        f"OP-000{number}", Stage.FIX, subject, kind, OperationExecutor.VCS, "部署后确认发现回归", True, f"k{number}", 1,
        status, NOW - timedelta(days=1), decided_at=NOW, result=result))


def failed_round(number):
    return {"round": number, "checksPassed": True, "reviews": [{"mode": "light", "passed": False}],
            "blockerCategories": ["local"], "risk": None, "discardedFindings": [],
            "failures": [{"check": "review", "location": "src/a.src:3", "problem": "没有处理空列表", "category": "local"}]}


def test_an_override_after_a_false_refute_is_written_from_the_earlier_record(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001")
    triaged(world, "P-0001", verdict=Verdict.REFUTED, disposition=Disposition.FALSE_POSITIVE,
            outcome=TriageOutcome.FALSE_REFUTE, outcome_at=NOW)
    triaged(world, "P-0001", attempt=2, outcome=TriageOutcome.OVERRIDDEN, outcome_at=NOW)
    (found,) = troubles.collect(world.conn, world.layout)
    assert (found.kind, found.keys, found.knowledge_type.value) == (
        "triage-misjudged", ("lesson:triage:P-0001:1",), "triage-lesson")
    assert "实际结果：误判为不成立" in found.text and "改判后的结论：确认成立" in found.text


def test_only_requested_changes_and_failed_review_rounds_count_as_rejections(tmp_path):
    world = make_learn_world(tmp_path)
    issue(world, "0007")
    reviews = [{"id": "R1", "author": {"login": "lead"}, "state": "CHANGES_REQUESTED", "body": "错误处理请统一用已有的包装"},
               {"id": "R2", "author": {"login": "lead"}, "state": "APPROVED", "body": "可以"},
               {"id": "R3", "author": {"login": "peer"}, "state": "COMMENTED", "body": "命名请用完整单词"}]
    pulls.save(world.conn, PullRecord("0007", 12, "u", "b", "修复订单查询", "OPEN", NOW, reviews=reviews))
    save_handoff(world, RunStage.FIX, "0007", fix_outputs(rounds=[failed_round(1)]), FIX_RUN)
    save_handoff(world, RunStage.FIX, "0007", fix_outputs(rounds=[failed_round(1)]), FIX_RUN, attempt=2)
    found = troubles.collect(world.conn, world.layout)
    assert kinds(found) == [("review-rejected", ("lesson:pr:0007:R1",)),
                            ("review-rejected", (f"lesson:review:0007:{FIX_RUN}-1",))]
    assert "错误处理请统一用已有的包装" in found[0].text and "命名" not in found[0].text
    assert "src/a.src:3 没有处理空列表" in found[1].text and found[1].knowledge_type.value == "fix-lesson"


def test_held_fixes_reverts_rejected_plans_and_user_closes_are_troubles(tmp_path):
    world = make_learn_world(tmp_path)
    issue(world, "0007", status=IssueStatus.TODO)
    issue_events.append(world.conn, IssueEventRecord("0007", NOW, IssueEvent.FIX_HELD.value, "auto",
                                                     note="写代码三轮仍未通过评审"))
    issue_events.append(world.conn, IssueEventRecord("0007", NOW, IssueEvent.USER_CLOSED.value, "user",
                                                     close_reason=CloseReason.WONT_FIX, note="按设计如此"))
    operation(world, 1, OperationKind.REVERT_PULL_REQUEST, OperationStatus.EXECUTED)
    operation(world, 2, OperationKind.REVERT_PULL_REQUEST, OperationStatus.REJECTED)
    operation(world, 3, OperationKind.FIX_PLAN, OperationStatus.REJECTED, result={"note": "应该改入口而不是服务层"})
    operation(world, 4, OperationKind.FIX_PLAN, OperationStatus.CONFIRMED)
    found = troubles.collect(world.conn, world.layout)
    assert [item.kind for item in found] == ["fix-failed", "reverted", "plan-rejected", "issue-closed"]
    assert "写代码三轮仍未通过评审" in found[0].text
    assert "合并被撤销：部署后确认发现回归" in found[1].text
    assert "应该改入口而不是服务层" in found[2].text
    assert "按设计如此" in found[3].text and "不修" in found[3].text


def test_since_keeps_only_later_troubles(tmp_path):
    world = make_learn_world(tmp_path)
    problem_with(world, "P-0001")
    problem_with(world, "P-0002")
    triaged(world, "P-0001", outcome=TriageOutcome.FALSE_CONFIRM, outcome_at=NOW - timedelta(days=40))
    triaged(world, "P-0002", outcome=TriageOutcome.FALSE_CONFIRM, outcome_at=NOW)
    found = troubles.collect(world.conn, world.layout, NOW - timedelta(days=30))
    assert [item.subject_id for item in found] == ["P-0002"]
