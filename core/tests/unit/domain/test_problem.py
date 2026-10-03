from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tightrein.domain.enums import Probe, ProblemStatus, Source
from tightrein.domain.problem import (
    IgnoreCondition,
    ProblemScope,
    apply_occurrence,
    ignore_condition,
    new_problem,
    role_of,
    scope_of,
    title_for,
)
from tightrein.domain.signal import Signal

TITLE_LENGTH = 120

T = datetime(2026, 9, 29, 2, 15, tzinfo=timezone.utc)


def sig(probe, check, location, message="m", context=None, actor=None, normalized=None, at=T, release="c1"):
    return Signal(
        id="S-" + "0" * 26, run_id="R-20260929-021500-collect-x", source=Source.SYNTHETIC, probe=probe,
        check=check, environment="staging", occurred_at=at, release=release, location=location,
        message=message, context=context or {}, actor=actor or {}, normalized_message=normalized,
    )


def api_signal(**kwargs):
    return sig(Probe.API_FUZZ, "not_a_server_error", "POST /api/Order/Query/42",
               context={"response": {"status": 500}}, actor={"role": "Company"}, **kwargs)


def test_title_api_fuzz():
    assert title_for(api_signal(), TITLE_LENGTH) == "POST /api/Order/Query/{id} not_a_server_error 500"


def test_title_api_fuzz_authorization_includes_role():
    s = sig(Probe.API_FUZZ, "unauthorized_role_access", "GET /api/User",
            context={"role": "Personal", "response": {"status": 200}})
    assert title_for(s, TITLE_LENGTH) == "GET /api/User unauthorized_role_access 200(Personal)"


def test_title_platform_static_incidental():
    log = sig(Probe.PLATFORM_ERRORS, "error", "OrderService.Query", normalized="NullReferenceException: <value>",
              context={"exceptionType": "NullReferenceException"})
    static = sig(Probe.STATIC, "missing-owner-check", "Services/OrderService.cs:OrderService.Query:88")
    incidental = sig(Probe.INCIDENTAL, "incidental", "Services/A.cs:A.B", message="未校验归属")
    plain = sig(Probe.PLATFORM_ERRORS, "critical", "Microsoft.Hosting", normalized="Host stopped",
                context={"category": "Microsoft.Hosting"})
    assert title_for(log, TITLE_LENGTH) == "NullReferenceException: <value>"
    assert title_for(plain, TITLE_LENGTH) == "Microsoft.Hosting：Host stopped"
    assert title_for(static, TITLE_LENGTH) == "missing-owner-check：Services/OrderService.cs:OrderService.Query"
    assert title_for(incidental, TITLE_LENGTH) == "Services/A.cs:A.B：未校验归属"
    tracked = sig(Probe.PLATFORM_ERRORS, "frontend-error", "src/app.js:render", normalized="TypeError: x",
                  context={"platformGroup": "sentry:acme/1"})
    alert = sig(Probe.ALERTS, "business-alert", "OrdersStalled", message="订单 1 小时没有进展")
    assert title_for(tracked, TITLE_LENGTH) == "TypeError: x"
    assert title_for(alert, TITLE_LENGTH) == "OrdersStalled：订单 1 小时没有进展"


def test_title_is_truncated():
    s = sig(Probe.INCIDENTAL, "incidental", "A.cs:A.B", message="x" * 300)
    assert len(title_for(s, TITLE_LENGTH)) == TITLE_LENGTH


def test_role_from_actor_or_context():
    assert role_of(sig(Probe.STATIC, "r", "a.py:f", actor={"role": "Company"})) == "Company"
    assert role_of(sig(Probe.API_FUZZ, "x", "GET /a", context={"role": "Personal"})) == "Personal"
    assert role_of(sig(Probe.PLATFORM_ERRORS, "error", "A.B")) is None


def test_scope_records_the_source_name():
    s = sig(Probe.PROJECT_PROBE, "daily-import", "job:import", context={"sourceName": "daily-import"})
    assert scope_of(s) == ProblemScope(location="job:import", source="daily-import")
    assert scope_of(api_signal()).source is None


def test_new_problem_is_pending():
    problem = new_problem("P-0001", api_signal(), "abcd", 1, TITLE_LENGTH)
    assert problem.status is ProblemStatus.PENDING
    assert problem.occurrences == 1
    assert problem.first_seen_release == problem.last_seen_release == "c1"
    assert problem.scope.roles == frozenset({"Company"})
    assert problem.title == "POST /api/Order/Query/{id} not_a_server_error 500"


def test_apply_occurrence_accumulates():
    problem = new_problem("P-0001", api_signal(), "abcd", 1, TITLE_LENGTH)
    later = sig(Probe.API_FUZZ, "not_a_server_error", "POST /api/Order/Query/9",
                context={"response": {"status": 500}}, actor={"role": "Personal"},
                at=T + timedelta(hours=1), release="c2")
    updated = apply_occurrence(problem, later)
    assert updated.occurrences == 2
    assert updated.last_seen_at == T + timedelta(hours=1)
    assert updated.last_seen_release == "c2"
    assert updated.first_seen_release == "c1"
    assert updated.scope.roles == frozenset({"Company", "Personal"})


def test_apply_occurrence_out_of_order_and_resets_clean_runs():
    problem = new_problem("P-0001", api_signal(), "abcd", 1, TITLE_LENGTH)
    problem = replace(problem, clean_covered_runs=2)
    earlier = api_signal(at=T - timedelta(hours=1), release="c0")
    updated = apply_occurrence(problem, earlier)
    assert updated.first_seen_at == T - timedelta(hours=1)
    assert updated.first_seen_release == "c0"
    assert updated.last_seen_release == "c1"
    assert updated.clean_covered_runs == 0


def test_apply_occurrence_keeps_release_when_signal_has_none():
    problem = new_problem("P-0001", api_signal(), "abcd", 1, TITLE_LENGTH)
    updated = apply_occurrence(problem, api_signal(at=T + timedelta(minutes=1), release=None))
    assert updated.last_seen_release == "c1"


def test_ignore_condition_round_trip_and_baseline():
    problem = new_problem("P-0001", api_signal(), "abcd", 1, TITLE_LENGTH)
    condition = ignore_condition(problem, until=T + timedelta(days=7), occurrences=5, new_release=True)
    assert condition.baseline_occurrences == 1
    assert condition.baseline_release == "c1"
    assert IgnoreCondition.from_dict(condition.to_dict()) == condition
    assert condition.to_dict()["until"] == "2026-10-06T02:15:00Z"


def test_ignore_condition_permanent_and_validation():
    assert IgnoreCondition().permanent
    assert not IgnoreCondition(severity_escalated=True).permanent
    with pytest.raises(ValueError):
        IgnoreCondition(occurrences=0)


def test_scope_round_trip():
    scope = ProblemScope("GET /a", frozenset({"B", "A"}), "error-tracking")
    assert scope.to_dict()["roles"] == ["A", "B"]
    assert ProblemScope.from_dict(scope.to_dict()) == scope


def test_problem_document_matches_the_schema():
    from tightrein.contracts import validate as contracts

    problem = replace(new_problem("P-0001", api_signal(), "0123456789abcdef", 1, TITLE_LENGTH),
                      ignore_until=IgnoreCondition(occurrences=2))
    document = problem.to_dict()
    assert contracts.validate("data/problem.schema.json", document) == []
    assert document["scope"]["roles"] == ["Company"]
    assert document["ignoreUntil"]["occurrences"] == 2
