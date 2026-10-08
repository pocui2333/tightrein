from datetime import UTC, datetime

from tightrein.collect.dedup import reproduce
from tightrein.collect.dedup.changes import ChangeSet
from tightrein.collect.dedup.group import Params as GroupParams
from tightrein.collect.dedup.group import apply as group_apply
from tightrein.collect.dedup.reproduce import (
    NOT_REPRODUCED,
    REPRODUCED,
    UNAVAILABLE,
    Params,
    apply,
    judge,
)
from tightrein.collect.dedup.status import Event

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
PARAMS = Params(replay_checks={"collect.api_fuzz": frozenset({"server_error"})}, attempts=2, watch_occurrences=2)


def grouped(signals) -> ChangeSet:
    found = ChangeSet(run="R-20261007T120000Z-collect", now=NOW, next_number=1)
    group_apply(found, signals, {signal.id: signal.message for signal in signals},
                GroupParams(title_length=120, nearby_lines=10))
    return found


def replayers(*answers):
    calls = []

    def replay(evidence):
        calls.append(evidence)
        return answers[len(calls) - 1]

    return (lambda source: replay if source == "collect.api_fuzz" else None), calls


def fuzz(make_signal, **kwargs):
    return make_signal(source="collect.api_fuzz", check_type="server_error", location="GET /api/x",
                       evidence={"status": 500, "request": "r"}, deterministic=True, **kwargs)


def test_judge_replays():
    assert judge([NOT_REPRODUCED, REPRODUCED]) is Event.CONFIRMED
    assert judge([UNAVAILABLE, REPRODUCED]) is Event.CONFIRMED
    assert judge([UNAVAILABLE, NOT_REPRODUCED]) is None
    assert judge([NOT_REPRODUCED, NOT_REPRODUCED]) is Event.NOT_REPRODUCED


def test_server_errors_are_replayed_and_any_reproduction_confirms(make_signal):
    found = grouped([fuzz(make_signal)])
    find, calls = replayers(NOT_REPRODUCED, REPRODUCED)
    apply(found, [], {}, find, PARAMS)
    assert found.known["P-0001"].status == "new" and len(calls) == 2 and calls[0]["request"] == "r"
    assert found.reproduction["P-0001"].result == REPRODUCED


def test_not_reproduced_becomes_intermittent_and_unavailable_stays_pending(make_signal):
    found = grouped([fuzz(make_signal)])
    apply(found, [], {}, replayers(NOT_REPRODUCED, NOT_REPRODUCED)[0], PARAMS)
    assert found.known["P-0001"].status == "intermittent"
    pending = grouped([fuzz(make_signal)])
    apply(pending, [], {}, replayers(UNAVAILABLE, NOT_REPRODUCED)[0], PARAMS)
    assert pending.known["P-0001"].status == "pending" and pending.transitions == []


def test_without_a_replayer_replay_problems_stay_pending(make_signal):
    found = grouped([fuzz(make_signal)])
    apply(found, [], {}, lambda source: None, PARAMS)
    assert found.known["P-0001"].status == "pending" and found.notes


def test_reproducible_signals_and_other_checks_are_not_replayed(make_signal):
    found = grouped([fuzz(make_signal, reproducible=True)])
    find, calls = replayers()
    apply(found, [], {}, find, PARAMS)
    assert found.known["P-0001"].status == "new" and calls == []


def test_deterministic_and_verified_are_valid_on_first_sight(make_signal):
    found = grouped([make_signal(source="collect.static", check_type="static", location="a.py:1", verified=True),
                     make_signal(source="collect.alerts", check_type="alert", location=None, group_key="am:1",
                                 deterministic=True)])
    apply(found, [], {}, lambda source: None, PARAMS)
    assert [found.known[item].status for item in ("P-0001", "P-0002")] == ["new", "new"]


def test_one_off_runtime_errors_are_watched_until_they_repeat(make_signal):
    once = grouped([make_signal(group_key="g1")])
    apply(once, [], {}, lambda source: None, PARAMS)
    assert once.known["P-0001"].status == "watching"
    twice = grouped([make_signal(group_key="g2"), make_signal(group_key="g2")])
    apply(twice, [], {}, lambda source: None, PARAMS)
    assert twice.known["P-0001"].status == "new"


def test_stored_pending_problems_are_judged_again_with_their_latest_record(make_signal):
    stored = grouped([fuzz(make_signal)])
    problem = stored.known["P-0001"]
    later = ChangeSet(run="R-20261007T130000Z-collect", now=NOW, next_number=2)
    later.load([problem])
    find, calls = replayers(REPRODUCED, REPRODUCED)
    record = {"evidence": {"status": 500, "request": "old"}, "deterministic": True}
    apply(later, [problem], {"P-0001": record}, find, PARAMS)
    assert later.known["P-0001"].status == "new" and calls[0]["request"] == "old"


def test_replay_module_is_found_by_source_name():
    assert reproduce.replayer_for("collect.no_such_source", lambda replay: replay) is None
