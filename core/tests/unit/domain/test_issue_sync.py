from datetime import datetime, timezone

import pytest

from tightrein.domain.enums import (
    CloseReason,
    IssueEvent,
    IssuePhase,
    IssueStatus,
    Probe,
    Severity,
    ProblemEffect,
    ProblemEvent,
    ProblemStatus,
)
from tightrein.domain.issue import Issue, event_for_regression, problem_context_for_close
from tightrein.domain.problem import IgnoreCondition, Problem, ProblemScope, transition

T = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)
PROBLEM = Problem(
    id="P-0001", fingerprint="f" * 16, fingerprint_version=1, probe=Probe.API_FUZZ, title="t",
    status=ProblemStatus.ONGOING, first_seen_at=T, last_seen_at=T, scope=ProblemScope("GET /a"),
    last_seen_release="c1", occurrences=3, issue_id="0007",
)


def sync_problem(problem, reason, duplicate_of=None):
    # 与 pipeline/issue/steps/transitions 的关闭同步相同：以关闭原因构造上下文，按 issue-closed 转换
    return transition(problem.status, ProblemEvent.ISSUE_CLOSED, problem_context_for_close(problem, reason, duplicate_of))


@pytest.mark.parametrize("reason", [CloseReason.WONT_FIX, CloseReason.FIX_REJECTED])
def test_wont_fix_and_rejected_ignore_until_escalation_or_new_release(reason):
    state, effects = sync_problem(PROBLEM, reason)
    assert state is ProblemStatus.IGNORED
    expected = IgnoreCondition(new_release=True, severity_escalated=True, baseline_occurrences=3,
                               baseline_release="c1")
    assert effects[0].kind is ProblemEffect.SET_IGNORE_UNTIL
    assert effects[0].detail == {"ignore_until": expected}


def test_not_a_bug_becomes_false_positive():
    state, effects = sync_problem(PROBLEM, CloseReason.NOT_A_BUG)
    assert state is ProblemStatus.IGNORED
    assert [effect.kind for effect in effects] == [ProblemEffect.CLEAR_IGNORE_UNTIL, ProblemEffect.CREATE_SUPPRESSION]


def test_duplicate_moves_problem_to_other_issue():
    state, effects = sync_problem(PROBLEM, CloseReason.DUPLICATE, duplicate_of="0003")
    assert state is ProblemStatus.ONGOING
    assert effects[0].kind is ProblemEffect.MOVE_TO_DUPLICATE_ISSUE
    assert effects[0].detail == {"duplicate_of": "0003"}


def test_fixed_keeps_problem_state():
    assert sync_problem(PROBLEM, CloseReason.FIXED) == (ProblemStatus.ONGOING, ())


def test_context_for_close():
    assert problem_context_for_close(PROBLEM, CloseReason.FIXED).ignore_until is None
    assert problem_context_for_close(PROBLEM, CloseReason.WONT_FIX).ignore_until is not None


@pytest.mark.parametrize("fields,expected", [
    (dict(status=IssueStatus.DONE, close_reason=CloseReason.FIXED), IssueEvent.PROBLEM_REGRESSED),
    (dict(status=IssueStatus.CANCELLED, close_reason=CloseReason.WONT_FIX), IssueEvent.PROBLEM_REGRESSED),
    (dict(status=IssueStatus.DONE, close_reason=CloseReason.FIXED, phase=IssuePhase.DEPLOY_CHECK),
     IssueEvent.STAGING_FAILED),
    (dict(status=IssueStatus.IN_PROGRESS, phase=IssuePhase.FIX), None),
    (dict(status=IssueStatus.NEEDS_DECISION), None),
])
def test_event_for_regression(fields, expected):
    issue = Issue(id="0007", slug="x", title="x", severity=Severity.P1, created_at=T, updated_at=T, **fields)
    assert event_for_regression(issue) is expected
