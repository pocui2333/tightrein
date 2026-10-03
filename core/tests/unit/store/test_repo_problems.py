import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.enums import Probe, ProblemEvent, ProblemStatus
from tightrein.domain.problem import ProblemScope
from tightrein.store.repos import problem_events, problems, runs, signals
from tightrein.store.repos.problem_events import OPERATION_AUTO, OPERATION_USER, ProblemEventRecord

from store_samples import T0, ignored_problem, problem, run, signal


def test_problem_round_trip(conn):
    problems.save(conn, problem())
    problems.save(conn, ignored_problem())
    assert problems.get(conn, "P-0001") == problem()
    assert problems.get(conn, "P-0002") == ignored_problem()
    assert problems.get(conn, "P-0404") is None


def test_saving_again_updates_the_problem(conn):
    problems.save(conn, problem())
    problems.save(conn, problem(status=ProblemStatus.ONGOING, occurrences=4, issue_id="0007"))
    stored = problems.get(conn, "P-0001")
    assert (stored.status, stored.occurrences, stored.issue_id) == (ProblemStatus.ONGOING, 4, "0007")


def test_invalid_scope_is_rejected(conn):
    with pytest.raises(SchemaValidationError, match=r"\$\.location"):
        problems.save(conn, problem(scope=ProblemScope("")))
    assert problems.get(conn, "P-0001") is None


def test_fingerprint_is_unique(conn):
    problems.save(conn, problem())
    with pytest.raises(sqlite3.IntegrityError):
        problems.save(conn, problem("P-0002"))


def test_find_by_statuses_probe_and_issue(conn):
    problems.save(conn, problem("P-0010", "f10", status=ProblemStatus.REGRESSED))
    problems.save(conn, problem("P-0002", "f2", probe=Probe.ALERTS, issue_id="0007"))
    problems.save(conn, ignored_problem("P-0003", "f3"))
    assert [item.id for item in problems.find(conn)] == ["P-0002", "P-0003", "P-0010"]
    found = problems.find(conn, statuses=[ProblemStatus.NEW, ProblemStatus.REGRESSED])
    assert [item.id for item in found] == ["P-0002", "P-0010"]
    assert [item.id for item in problems.find(conn, probe=Probe.ALERTS, issue_id="0007")] == ["P-0002"]
    assert problems.find(conn, statuses=[]) == []


def test_problem_signals_are_recorded_once_and_need_existing_signals(conn):
    runs.save(conn, run())
    signals.save_all(conn, [signal(1), signal(2)])
    problems.save(conn, problem())
    problems.add_signals(conn, "P-0001", [signal(2).id, signal(1).id])
    problems.add_signals(conn, "P-0001", [signal(1).id])
    assert problems.signal_ids(conn, "P-0001") == [signal(1).id, signal(2).id]
    with pytest.raises(sqlite3.IntegrityError):
        problems.add_signals(conn, "P-0001", ["S-missing"])


def test_by_fingerprint_follows_aliases(conn, clock):
    problems.save(conn, problem())
    problems.add_alias(conn, "old-fingerprint", "P-0001", clock)
    assert problems.by_fingerprint(conn, "a1b2c3d4e5f60718").id == "P-0001"
    assert problems.by_fingerprint(conn, "old-fingerprint").id == "P-0001"
    assert problems.by_fingerprint(conn, "unknown") is None
    assert problems.aliases(conn, "P-0001") == ["old-fingerprint"]


def test_merge_moves_signals_and_aliases(conn, clock):
    runs.save(conn, run())
    signals.save_all(conn, [signal(1), signal(2), signal(3)])
    target, source = problem("P-0001", "f1"), problem("P-0005", "f5")
    for item in (target, source, problem("P-0003", "f3"), problem("P-0009", "f9")):
        problems.save(conn, item)
    problems.add_signals(conn, "P-0001", [signal(1).id])
    problems.add_signals(conn, "P-0005", [signal(1).id, signal(2).id])
    problems.add_alias(conn, "f5-old", "P-0005", clock)

    merged = problems.merge(conn, "P-0001", "P-0005", clock)

    assert merged == replace(source, merged_into="P-0001")
    assert problems.get(conn, "P-0005").merged_into == "P-0001"
    assert problems.signal_ids(conn, "P-0001") == [signal(1).id, signal(2).id]
    assert problems.signal_ids(conn, "P-0005") == []
    assert problems.aliases(conn, "P-0001") == ["f5", "f5-old"]
    assert problems.by_fingerprint(conn, "f5").id == "P-0001"


def test_merge_rejections(conn, clock):
    problems.save(conn, problem("P-0001", "f1"))
    problems.save(conn, problem("P-0002", "f2", issue_id="0007"))
    problems.save(conn, problem("P-0003", "f3", merged_into="P-0001"))
    problems.save(conn, problem("P-0004", "f4"))
    cases = [("P-0001", "P-0001", "自身"), ("P-0001", "P-0404", "不存在"), ("P-0001", "P-0002", "0007"),
             ("P-0004", "P-0003", "已并入 P-0001"), ("P-0003", "P-0004", "应并入 P-0001")]
    for target, source, message in cases:
        with pytest.raises(problems.MergeRejected, match=message):
            problems.merge(conn, target, source, clock)
    assert problems.get(conn, "P-0004").merged_into is None


def event(at, name=ProblemEvent.REPRODUCED, **changes):
    values = dict(problem_id="P-0001", at=at, event=name, operation=OPERATION_AUTO,
                  from_status=ProblemStatus.PENDING, to_status=ProblemStatus.NEW)
    values.update(changes)
    return ProblemEventRecord(**values)


def test_problem_events_append_and_read_in_time_order(conn):
    problems.save(conn, problem())
    later = problem_events.append(conn, event(T0 + timedelta(minutes=1), ProblemEvent.USER_IGNORED,
                                              operation=OPERATION_USER, reason="已知问题", detail={"until": None}))
    earlier = problem_events.append(conn, event(T0, run_id="R-20260929-021503-aggregate"))
    stored = problem_events.for_problem(conn, "P-0001")
    assert [item.id for item in stored] == [earlier, later]
    assert stored[1] == event(T0 + timedelta(minutes=1), ProblemEvent.USER_IGNORED, operation=OPERATION_USER,
                              reason="已知问题", detail={"until": None}, id=later)
    with pytest.raises(ValueError):
        problem_events.append(conn, event(T0, id=99))
    with pytest.raises(ValueError):
        event(T0, operation="manual")


def test_unhandled_events_and_mark_handled(conn):
    problems.save(conn, problem())
    retriage = problem_events.append(conn, event(T0, ProblemEvent.RETRIAGE_REQUESTED, to_status=None))
    problem_events.append(conn, event(T0))
    assert [item.id for item in problem_events.unhandled(conn, ProblemEvent.RETRIAGE_REQUESTED)] == [retriage]
    problem_events.mark_handled(conn, retriage, T0 + timedelta(hours=1))
    assert problem_events.unhandled(conn, ProblemEvent.RETRIAGE_REQUESTED) == []
    assert problem_events.for_problem(conn, "P-0001")[0].handled_at == T0 + timedelta(hours=1)
