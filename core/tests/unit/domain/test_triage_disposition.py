from datetime import datetime, timezone

import pytest

from tightrein.domain.enums import Disposition, IssueLabel, Probe, ProblemStatus, Severity, Treatment, Verdict
from tightrein.domain.problem import IgnoreCondition, Problem, ProblemScope
from tightrein.domain.triage import DispositionFacts, deferred_condition, disposition, labels

CONFIRMED = Verdict.CONFIRMED


@pytest.mark.parametrize("facts,expected", [
    (DispositionFacts(Verdict.INSUFFICIENT, Severity.P1), Disposition.MANUAL_QUEUE),
    (DispositionFacts(CONFIRMED, Severity.P1, Treatment.IMMEDIATE, needs_manual=True), Disposition.MANUAL_QUEUE),
    (DispositionFacts(Verdict.REFUTED, Severity.P2), Disposition.FALSE_POSITIVE),
    (DispositionFacts(Verdict.REFUTED, Severity.P0, refuter_verdict=Verdict.REFUTED), Disposition.FALSE_POSITIVE),
    (DispositionFacts(CONFIRMED, Severity.P1, fixed_on_main=True), Disposition.AWAITING_DEPLOY),
    (DispositionFacts(CONFIRMED, Severity.P2, tradeoff_hit=True), Disposition.ACCEPTED_TRADEOFF),
    (DispositionFacts(CONFIRMED, Severity.P2, Treatment.IMMEDIATE), Disposition.CREATE_ISSUE),
    (DispositionFacts(Verdict.CONDITIONAL, Severity.P2, Treatment.SCHEDULED), Disposition.CREATE_ISSUE),
    (DispositionFacts(CONFIRMED, Severity.P3, Treatment.OBSERVE), Disposition.DEFERRED),
    (DispositionFacts(CONFIRMED, Severity.P3, Treatment.WONT_FIX), Disposition.ACCEPTED_TRADEOFF),
])
def test_disposition_rows(facts, expected):
    assert disposition(facts) is expected


@pytest.mark.parametrize("facts", [
    DispositionFacts(CONFIRMED, Severity.P0, Treatment.WONT_FIX),
    DispositionFacts(Verdict.CONDITIONAL, Severity.P0, tradeoff_hit=True),
    DispositionFacts(CONFIRMED, Severity.P0),
])
def test_p0_confirmed_always_creates_issue(facts):
    assert disposition(facts) is Disposition.CREATE_ISSUE


def test_p0_fixed_on_main_waits_for_deploy():
    assert disposition(DispositionFacts(CONFIRMED, Severity.P0, fixed_on_main=True)) is Disposition.AWAITING_DEPLOY


def test_p0_refuted_requires_refuter():
    with pytest.raises(ValueError):
        disposition(DispositionFacts(Verdict.REFUTED, Severity.P0))


def test_confirmed_without_treatment_is_an_error():
    with pytest.raises(ValueError):
        disposition(DispositionFacts(CONFIRMED, Severity.P2))


def test_labels_discuss_when_protected():
    assert labels(True) == (IssueLabel.DISCUSS_WITH_AUTHOR,)
    assert labels(False) == ()


def test_deferred_condition():
    at = datetime(2026, 9, 29, tzinfo=timezone.utc)
    problem = Problem(id="P-0001", fingerprint="f" * 16, fingerprint_version=1, probe=Probe.API_FUZZ, title="t",
                      status=ProblemStatus.NEW, first_seen_at=at, last_seen_at=at, scope=ProblemScope("GET /a"),
                      last_seen_release="c1", occurrences=4)
    assert deferred_condition(problem, 5) == IgnoreCondition(occurrences=5, severity_escalated=True,
                                                             baseline_occurrences=4, baseline_release="c1")
