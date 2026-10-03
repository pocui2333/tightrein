from datetime import datetime, timezone

import pytest

from tightrein.domain.enums import CloseReason, Disposition, ProblemEffect, ProblemEvent, ProblemStatus
from tightrein.domain.problem import IgnoreCondition, ProblemContext, transition
from tightrein.domain.state_machine import InvalidTransition, SideEffect

S = ProblemStatus
E = ProblemEvent
UNTIL = IgnoreCondition(until=datetime(2026, 10, 30, tzinfo=timezone.utc))


def kinds(effects):
    return [effect.kind for effect in effects]


def test_reproduction_confirms_or_marks_intermittent():
    assert transition(S.PENDING, E.REPRODUCED) == (S.NEW, ())
    state, effects = transition(S.PENDING, E.NOT_REPRODUCED)
    assert state is S.PENDING and kinds(effects) == [ProblemEffect.MARK_INTERMITTENT]


def test_intermittent_problem_seen_again_becomes_new():
    assert transition(S.PENDING, E.PROMOTED) == (S.NEW, ())
    with pytest.raises(InvalidTransition):
        transition(S.NEW, E.PROMOTED)


@pytest.mark.parametrize("state", [S.NEW, S.ONGOING, S.RESOLVED])
def test_reproduced_only_from_pending(state):
    with pytest.raises(InvalidTransition):
        transition(state, E.REPRODUCED)


@pytest.mark.parametrize("event", [E.SEEN_AGAIN, E.REGRESSION_CHECK_FAILED])
def test_occurrence_on_resolved_is_regression_only_on_newer_release(event):
    assert transition(S.RESOLVED, event, ProblemContext(regressed=True)) == (S.REGRESSED, ())
    assert transition(S.RESOLVED, event, ProblemContext(regressed=False)) == (S.RESOLVED, ())


def test_regression_with_issue_notifies_issue():
    state, effects = transition(S.RESOLVED, E.SEEN_AGAIN, ProblemContext(regressed=True, issue_id="0007"))
    assert state is S.REGRESSED
    assert effects == (SideEffect(ProblemEffect.ISSUE_REGRESSED, {"issue_id": "0007"}),)


@pytest.mark.parametrize("state", [S.PENDING, S.NEW, S.ONGOING, S.IGNORED, S.REGRESSED])
def test_seen_again_keeps_other_states(state):
    assert transition(state, E.SEEN_AGAIN) == (state, ())


@pytest.mark.parametrize("event", [E.COVERED_RUN_WITHOUT_OCCURRENCE, E.RESOLVED_ON_NEW_COMMIT])
@pytest.mark.parametrize("state", [S.NEW, S.ONGOING])
def test_resolution_records_release(event, state):
    new_state, effects = transition(state, event, ProblemContext(ready_to_resolve=True, release="c9"))
    assert new_state is S.RESOLVED
    assert effects == (SideEffect(ProblemEffect.RECORD_RESOLVED_RELEASE, {"release": "c9"}),)


def test_resolution_not_ready_keeps_state():
    assert transition(S.ONGOING, E.COVERED_RUN_WITHOUT_OCCURRENCE) == (S.ONGOING, ())
    assert transition(S.PENDING, E.COVERED_RUN_WITHOUT_OCCURRENCE, ProblemContext(ready_to_resolve=True)) == (
        S.PENDING, ())


def test_resolution_requires_release():
    with pytest.raises(ValueError, match="release"):
        transition(S.NEW, E.COVERED_RUN_WITHOUT_OCCURRENCE, ProblemContext(ready_to_resolve=True))


@pytest.mark.parametrize("disposition,expected,effects", [
    (Disposition.FALSE_POSITIVE, S.IGNORED, [ProblemEffect.CLEAR_IGNORE_UNTIL, ProblemEffect.CREATE_SUPPRESSION]),
    (Disposition.ACCEPTED_TRADEOFF, S.IGNORED, [ProblemEffect.CLEAR_IGNORE_UNTIL]),
    (Disposition.AWAITING_DEPLOY, S.ONGOING, [ProblemEffect.CLEAR_IGNORE_UNTIL]),
    (Disposition.CREATE_ISSUE, S.ONGOING, [ProblemEffect.CLEAR_IGNORE_UNTIL]),
    (Disposition.DEFERRED, S.IGNORED, [ProblemEffect.SET_IGNORE_UNTIL]),
    (Disposition.MANUAL_QUEUE, S.NEW, []),
])
def test_triaged_follows_disposition(disposition, expected, effects):
    context = ProblemContext(disposition=disposition, ignore_until=UNTIL)
    state, side_effects = transition(S.NEW, E.TRIAGED, context)
    assert state is expected
    assert kinds(side_effects) == effects


def test_deferred_carries_condition():
    context = ProblemContext(disposition=Disposition.DEFERRED, ignore_until=UNTIL)
    _, effects = transition(S.REGRESSED, E.TRIAGED, context)
    assert effects == (SideEffect(ProblemEffect.SET_IGNORE_UNTIL, {"ignore_until": UNTIL}),)


def test_triaged_requires_disposition_and_triageable_state():
    with pytest.raises(InvalidTransition):
        transition(S.NEW, E.TRIAGED)
    with pytest.raises(InvalidTransition):
        transition(S.IGNORED, E.TRIAGED, ProblemContext(disposition=Disposition.CREATE_ISSUE))


def test_override_can_revive_ignored_problem():
    context = ProblemContext(disposition=Disposition.CREATE_ISSUE)
    assert transition(S.IGNORED, E.OVERRIDDEN, context)[0] is S.ONGOING


@pytest.mark.parametrize("state", list(ProblemStatus))
def test_user_ignore_and_false_positive_from_any_state(state):
    assert transition(state, E.USER_IGNORED, ProblemContext(ignore_until=IgnoreCondition()))[0] is S.IGNORED
    assert transition(state, E.USER_FALSE_POSITIVE)[0] is S.IGNORED


def test_user_reopen_only_resolved_or_ignored():
    state, effects = transition(S.RESOLVED, E.USER_REOPENED)
    assert state is S.NEW
    assert kinds(effects) == [ProblemEffect.CLEAR_IGNORE_UNTIL, ProblemEffect.RESET_CLEAN_RUNS]
    with pytest.raises(InvalidTransition):
        transition(S.ONGOING, E.USER_REOPENED)


def test_ignore_expired_returns_to_new():
    assert transition(S.IGNORED, E.IGNORE_EXPIRED)[0] is S.NEW
    with pytest.raises(InvalidTransition):
        transition(S.NEW, E.IGNORE_EXPIRED)


def test_merged_keeps_state_and_names_target():
    state, effects = transition(S.NEW, E.MERGED, ProblemContext(merge_target="P-0001"))
    assert state is S.NEW
    assert effects == (SideEffect(ProblemEffect.MERGE_INTO, {"merge_target": "P-0001"}),)
    with pytest.raises(ValueError):
        transition(S.NEW, E.MERGED)


def test_rebuilt_and_retriage_keep_state():
    assert transition(S.ONGOING, E.REBUILT) == (S.ONGOING, ())
    assert transition(S.IGNORED, E.RETRIAGE_REQUESTED) == (S.IGNORED, ())
    with pytest.raises(InvalidTransition):
        transition(S.PENDING, E.RETRIAGE_REQUESTED)


@pytest.mark.parametrize("reason,expected,effects", [
    (CloseReason.FIXED, S.RESOLVED, []),
    (CloseReason.FIX_REJECTED, S.IGNORED, [ProblemEffect.SET_IGNORE_UNTIL]),
    (CloseReason.WONT_FIX, S.IGNORED, [ProblemEffect.SET_IGNORE_UNTIL]),
    (CloseReason.NOT_A_BUG, S.IGNORED, [ProblemEffect.CLEAR_IGNORE_UNTIL, ProblemEffect.CREATE_SUPPRESSION]),
    (CloseReason.DUPLICATE, S.RESOLVED, [ProblemEffect.MOVE_TO_DUPLICATE_ISSUE]),
])
def test_issue_closed_syncs_problem(reason, expected, effects):
    context = ProblemContext(close_reason=reason, ignore_until=UNTIL, duplicate_of="0003")
    state, side_effects = transition(S.RESOLVED, E.ISSUE_CLOSED, context)
    assert state is expected
    assert kinds(side_effects) == effects


def test_issue_closed_requires_reason():
    with pytest.raises(InvalidTransition):
        transition(S.ONGOING, E.ISSUE_CLOSED)
