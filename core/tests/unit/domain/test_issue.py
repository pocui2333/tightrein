from datetime import datetime, timezone

import pytest

from tightrein.domain.enums import (
    CloseReason,
    IssueEffect,
    IssueEvent,
    IssuePhase,
    IssueStatus,
    Severity,
    Stage,
    TriageOutcome,
)
from tightrein.domain.issue import OPEN, Hold, Issue, IssueContext, legacy_status, transition
from tightrein.domain.state_machine import InvalidTransition, SideEffect

S = IssueStatus
E = IssueEvent
T = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)
HOLD = Hold(reason="连续 2 次合并前验证失败", stage=Stage.VERIFY, since=T)


def kinds(effects):
    return [effect.kind for effect in effects]


P = IssuePhase


def at(phase):
    return IssueContext(phase=phase)


@pytest.mark.parametrize("event,source,phase,target,new_phase", [
    (E.APPROVE, S.NEEDS_DECISION, None, S.TODO, None),
    (E.FIX_STARTED, S.TODO, None, S.IN_PROGRESS, P.FIX),
    (E.FIX_STARTED, S.IN_PROGRESS, P.VERIFY, S.IN_PROGRESS, P.FIX),
    (E.FIX_STARTED, S.PENDING_MERGE, None, S.IN_PROGRESS, P.FIX),
    (E.NOT_REPRODUCED, S.IN_PROGRESS, P.FIX, S.NEEDS_DECISION, None),
    (E.FIX_DONE, S.IN_PROGRESS, P.FIX, S.IN_PROGRESS, P.VERIFY),
    (E.VERIFY_PASSED, S.IN_PROGRESS, P.VERIFY, S.IN_PROGRESS, P.SUBMIT),
    (E.VERIFY_FAILED, S.IN_PROGRESS, P.VERIFY, S.TODO, None),
    (E.MAIN_MERGED, S.IN_PROGRESS, P.SUBMIT, S.IN_PROGRESS, P.VERIFY),
    (E.MAIN_MERGED, S.PENDING_MERGE, None, S.IN_PROGRESS, P.VERIFY),
    (E.PR_CREATED, S.IN_PROGRESS, P.SUBMIT, S.PENDING_MERGE, None),
    (E.PR_MERGED, S.PENDING_MERGE, None, S.DONE, P.DEPLOY_CHECK),
    (E.PR_CLOSED, S.PENDING_MERGE, None, S.CANCELLED, None),
    (E.STAGING_VERIFIED, S.DONE, P.DEPLOY_CHECK, S.DONE, None),
    (E.STAGING_FAILED, S.DONE, P.DEPLOY_CHECK, S.TODO, None),
    (E.PROBLEM_REGRESSED, S.DONE, None, S.TODO, None),
    (E.USER_REOPENED, S.CANCELLED, None, S.TODO, None),
])
def test_transition_table(event, source, phase, target, new_phase):
    state, effects = transition(source, event, at(phase))
    assert state is target
    phases = [effect.detail["phase"] for effect in effects if effect.kind is IssueEffect.SET_PHASE]
    assert phases in ([new_phase], []) and (phases or new_phase is None)


@pytest.mark.parametrize("event,source,phase", [
    (E.APPROVE, S.TODO, None),
    (E.FIX_STARTED, S.DONE, P.DEPLOY_CHECK),
    (E.FIX_DONE, S.TODO, None),
    (E.FIX_DONE, S.IN_PROGRESS, P.VERIFY),
    (E.PR_CREATED, S.IN_PROGRESS, P.VERIFY),
    (E.PR_MERGED, S.IN_PROGRESS, P.SUBMIT),
    (E.STAGING_VERIFIED, S.PENDING_MERGE, None),
    (E.STAGING_VERIFIED, S.DONE, None),
    (E.USER_REOPENED, S.TODO, None),
])
def test_illegal_transitions(event, source, phase):
    with pytest.raises(InvalidTransition):
        transition(source, event, at(phase))


def test_fix_held_sets_hold():
    state, effects = transition(S.IN_PROGRESS, E.FIX_HELD, IssueContext(hold=HOLD, phase=P.FIX))
    assert state is S.NEEDS_DECISION
    assert effects == (SideEffect(IssueEffect.SET_HOLD, {"hold": HOLD}),)
    with pytest.raises(ValueError):
        transition(S.IN_PROGRESS, E.FIX_HELD, at(P.FIX))


def test_verify_failed_sets_hold_only_when_given():
    state, effects = transition(S.IN_PROGRESS, E.VERIFY_FAILED, IssueContext(hold=HOLD, phase=P.VERIFY))
    assert state is S.NEEDS_DECISION and effects[0].kind is IssueEffect.SET_HOLD
    assert transition(S.IN_PROGRESS, E.VERIFY_FAILED, at(P.VERIFY)) == (S.TODO, ())


def test_fix_started_clears_hold_and_not_reproduced_requests_retriage():
    assert kinds(transition(S.TODO, E.FIX_STARTED)[1]) == [IssueEffect.CLEAR_HOLD, IssueEffect.SET_PHASE]
    _, effects = transition(S.IN_PROGRESS, E.NOT_REPRODUCED, at(P.FIX))
    assert kinds(effects) == [IssueEffect.FILL_TRIAGE_OUTCOME, IssueEffect.REQUEST_RETRIAGE]
    assert effects[0].detail["outcome"] is TriageOutcome.FALSE_CONFIRM


def test_merge_closes_as_fixed_and_deploy_check_syncs_problems():
    _, effects = transition(S.PENDING_MERGE, E.PR_MERGED)
    assert kinds(effects) == [IssueEffect.CLOSE, IssueEffect.SET_PHASE]
    assert effects[0].detail["close_reason"] is CloseReason.FIXED
    _, effects = transition(S.DONE, E.STAGING_VERIFIED, at(P.DEPLOY_CHECK))
    assert kinds(effects) == [IssueEffect.SYNC_PROBLEMS, IssueEffect.FILL_TRIAGE_OUTCOME, IssueEffect.SET_PHASE]
    assert effects[0].detail["outcome"] is TriageOutcome.CORRECT
    _, effects = transition(S.PENDING_MERGE, E.PR_CLOSED)
    assert effects[0].detail == {"close_reason": CloseReason.FIX_REJECTED}


@pytest.mark.parametrize("source", sorted(OPEN, key=lambda item: item.value))
def test_user_close_from_any_open_state(source):
    state, effects = transition(source, E.USER_CLOSED, IssueContext(close_reason=CloseReason.WONT_FIX))
    assert state is S.CANCELLED
    assert effects[0].detail == {"close_reason": CloseReason.WONT_FIX}


def test_user_close_not_a_bug_and_duplicate():
    _, effects = transition(S.TODO, E.USER_CLOSED, IssueContext(close_reason=CloseReason.NOT_A_BUG))
    assert effects[-1] == SideEffect(IssueEffect.FILL_TRIAGE_OUTCOME,
                                     {"close_reason": CloseReason.NOT_A_BUG, "outcome": TriageOutcome.FALSE_CONFIRM})
    _, effects = transition(S.TODO, E.USER_CLOSED, IssueContext(close_reason=CloseReason.DUPLICATE,
                                                                  duplicate_of="0003"))
    assert effects[1].detail == {"close_reason": CloseReason.DUPLICATE, "duplicate_of": "0003"}
    with pytest.raises(ValueError):
        transition(S.TODO, E.USER_CLOSED, IssueContext(close_reason=CloseReason.DUPLICATE))


@pytest.mark.parametrize("reason", [None, CloseReason.FIXED, CloseReason.FIX_REJECTED])
def test_user_cannot_close_as_fixed_or_rejected(reason):
    with pytest.raises(InvalidTransition):
        transition(S.TODO, E.USER_CLOSED, IssueContext(close_reason=reason))


@pytest.mark.parametrize("source", [S.DONE, S.CANCELLED])
def test_user_cannot_close_closed_issue(source):
    with pytest.raises(InvalidTransition):
        transition(source, E.USER_CLOSED, IssueContext(close_reason=CloseReason.WONT_FIX))


@pytest.mark.parametrize("source,held", [(S.TODO, False), (S.IN_PROGRESS, False), (S.PENDING_MERGE, False),
                                         (S.NEEDS_DECISION, True)])
@pytest.mark.parametrize("target,phase", [(S.TODO, None), (S.IN_PROGRESS, P.VERIFY)])
def test_restart(source, held, target, phase):
    state, effects = transition(source, E.RESTART, IssueContext(restart_to=target, held=held))
    assert state is target
    assert kinds(effects) == [IssueEffect.CLEAR_HOLD, IssueEffect.SET_PHASE]
    assert effects[1].detail["phase"] is phase


@pytest.mark.parametrize("source", [S.NEEDS_DECISION, S.DONE, S.CANCELLED])
def test_restart_not_from_unapproved_or_closed(source):
    with pytest.raises(InvalidTransition):
        transition(source, E.RESTART, IssueContext(restart_to=S.TODO))


def test_restart_requires_target():
    with pytest.raises(InvalidTransition):
        transition(S.IN_PROGRESS, E.RESTART, IssueContext(restart_to=S.PENDING_MERGE))


def test_issue_invariants():
    base = dict(id="0007", slug="material-query-500", title="材料查询返回 500", severity=Severity.P1,
                created_at=T, updated_at=T)
    Issue(status=S.TODO, **base)
    Issue(status=S.DONE, close_reason=CloseReason.FIXED, phase=P.DEPLOY_CHECK, **base)
    Issue(status=S.CANCELLED, close_reason=CloseReason.WONT_FIX, **base)
    Issue(status=S.NEEDS_DECISION, hold=HOLD, **base)
    for bad in (dict(status=S.DONE), dict(status=S.TODO, close_reason=CloseReason.FIXED),
                dict(status=S.CANCELLED, close_reason=CloseReason.FIXED),
                dict(status=S.DONE, close_reason=CloseReason.WONT_FIX), dict(status=S.TODO, hold=HOLD),
                dict(status=S.IN_PROGRESS), dict(status=S.TODO, phase=P.FIX)):
        with pytest.raises(ValueError):
            Issue(**bad, **base)


@pytest.mark.parametrize("old,reason,held,expected", [
    ("needs-decision", None, False, (S.NEEDS_DECISION, None, None)),
    ("todo", None, True, (S.NEEDS_DECISION, None, None)),
    ("fixing", None, False, (S.IN_PROGRESS, P.FIX, None)),
    ("to-submit", None, False, (S.IN_PROGRESS, P.SUBMIT, None)),
    ("pr-review", None, False, (S.PENDING_MERGE, None, None)),
    ("merged", None, False, (S.DONE, P.DEPLOY_CHECK, CloseReason.FIXED)),
    ("closed", CloseReason.FIXED, False, (S.DONE, None, CloseReason.FIXED)),
    ("closed", CloseReason.WONT_FIX, False, (S.CANCELLED, None, CloseReason.WONT_FIX)),
    ("todo", None, False, (S.TODO, None, None)),
])
def test_legacy_status(old, reason, held, expected):
    assert legacy_status(old, reason, held) == expected


def test_hold_round_trip():
    assert Hold.from_dict(HOLD.to_dict()) == HOLD
    assert HOLD.to_dict()["since"] == "2026-09-29T02:15:00Z"
