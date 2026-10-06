from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tightrein.domain.enums import (
    CloseReason,
    Continuation,
    Disposition,
    IssueOrigin,
    IssuePhase,
    IssueStatus,
    ProblemStatus,
    Severity,
    Stage,
)
from tightrein.domain.issue import Hold, Issue
from tightrein.domain.next_step import ProblemProgress, next_step

T = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)
ISSUE = Issue(id="0007", slug="material-query-500", title="材料查询返回 500", status=IssueStatus.TODO,
              severity=Severity.P1, created_at=T, updated_at=T)


def progress(status, **kwargs):
    return ProblemProgress("P-0042", status, **kwargs)


@pytest.mark.parametrize("subject,module,command,continuation", [
    (progress(ProblemStatus.PENDING), Stage.AGGREGATE, "aggregate", Continuation.AUTO),
    (progress(ProblemStatus.NEW), Stage.TRIAGE, "triage", Continuation.AUTO),
    (progress(ProblemStatus.REGRESSED, disposition=Disposition.CREATE_ISSUE), Stage.TRIAGE, "triage",
     Continuation.AUTO),
    (progress(ProblemStatus.ONGOING, disposition=Disposition.CREATE_ISSUE), Stage.ISSUE, "issue create",
     Continuation.AUTO),
    (progress(ProblemStatus.NEW, disposition=Disposition.MANUAL_QUEUE), Stage.TRIAGE, "triage queue",
     Continuation.USER),
    (progress(ProblemStatus.ONGOING, disposition=Disposition.AWAITING_DEPLOY), Stage.AGGREGATE, "aggregate",
     Continuation.WAIT_DEPLOY),
    (progress(ProblemStatus.RESOLVED), None, None, Continuation.NONE),
    (progress(ProblemStatus.IGNORED, disposition=Disposition.FALSE_POSITIVE), None, None, Continuation.NONE),
])
def test_problem_steps(subject, module, command, continuation):
    step = next_step(subject)
    assert (step.module, step.command, step.continuation) == (module, command, continuation)


@pytest.mark.parametrize("status", [ProblemStatus.ONGOING, ProblemStatus.IGNORED, ProblemStatus.NEW])
def test_retriage_requested_goes_back_to_triage(status):
    step = next_step(progress(status, disposition=Disposition.CREATE_ISSUE, retriage_requested=True, issue=ISSUE))
    assert step.module is Stage.TRIAGE


def test_problem_with_issue_follows_issue():
    step = next_step(progress(ProblemStatus.ONGOING, disposition=Disposition.CREATE_ISSUE, issue=ISSUE))
    assert (step.module, step.command) == (Stage.FIX, "fix start")


@pytest.mark.parametrize("status,phase,module,command,continuation", [
    (IssueStatus.NEEDS_DECISION, None, Stage.ISSUE, "approve", Continuation.USER),
    (IssueStatus.TODO, None, Stage.FIX, "fix start", Continuation.INTERACTIVE),
    (IssueStatus.IN_PROGRESS, IssuePhase.FIX, Stage.FIX, "fix start", Continuation.INTERACTIVE),
    (IssueStatus.IN_PROGRESS, IssuePhase.VERIFY, Stage.VERIFY, "verify local", Continuation.AUTO),
    (IssueStatus.IN_PROGRESS, IssuePhase.SUBMIT, Stage.RELEASE, "release", Continuation.CONFIRM_EACH),
    (IssueStatus.PENDING_MERGE, None, None, None, Continuation.USER),
])
def test_issue_steps(status, phase, module, command, continuation):
    step = next_step(replace(ISSUE, status=status, phase=phase))
    assert (step.module, step.command, step.continuation) == (module, command, continuation)


def test_done_issue_waits_for_deploy_check_then_ends():
    waiting = replace(ISSUE, status=IssueStatus.DONE, close_reason=CloseReason.FIXED, phase=IssuePhase.DEPLOY_CHECK)
    step = next_step(waiting)
    assert (step.module, step.command, step.continuation) == (Stage.VERIFY, "verify staging", Continuation.WAIT_DEPLOY)
    assert step.can_continue
    for closed in (replace(waiting, phase=None),
                   replace(ISSUE, status=IssueStatus.CANCELLED, close_reason=CloseReason.WONT_FIX)):
        assert next_step(closed).continuation is Continuation.NONE
        assert not next_step(closed).can_continue


def test_hold_blocks_automatic_continue():
    held = replace(ISSUE, status=IssueStatus.NEEDS_DECISION, hold=Hold(reason="设计问题", stage=Stage.FIX, since=T))
    step = next_step(held)
    assert (step.module, step.command) == (Stage.FIX, "fix start")
    assert step.continuation is Continuation.USER
    assert step.reason == "待决定：设计问题"
    assert not step.can_continue


def test_can_continue():
    assert next_step(progress(ProblemStatus.NEW)).can_continue
    assert not next_step(replace(ISSUE, status=IssueStatus.NEEDS_DECISION)).can_continue


def test_a_manual_todo_issue_without_a_branch_is_approved_first():
    manual = replace(ISSUE, origin=IssueOrigin.MANUAL)
    step = next_step(manual)
    assert (step.command, step.continuation) == ("approve", Continuation.USER)
    assert next_step(replace(manual, branch="cty/fix-x")).command == "fix start"
    assert next_step(ISSUE).command == "fix start"
    queued = next_step(replace(manual, depends_on="0006"))
    assert (queued.command, queued.reason) == ("approve", "拆分出的后续子任务：排在 Issue 0006 之后，它合并后再放行")
