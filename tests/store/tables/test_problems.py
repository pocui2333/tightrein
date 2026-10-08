import sqlite3
from datetime import timedelta

from tightrein.protocol.naming import FixedClock
from tightrein.store.tables import occurrences, problems
from tightrein.store.tables.occurrences import Occurrence
from tightrein.store.tables.problems import Problem


def sample(clock: FixedClock, problem_id: str = "P-0001", fingerprint: str = "f1") -> Problem:
    return Problem(problem_id, fingerprint, "collect.platform_errors", "error", "new", "订单查询 500",
                   clock.now(), clock.now(), location="api/orders.py:42", extra={"aliases": ["f0"]})


def test_save_get_and_find_by_fingerprint(conn: sqlite3.Connection, clock: FixedClock) -> None:
    problem = sample(clock)
    problems.save(conn, problem, clock)
    assert problems.get(conn, "P-0001") == problem
    assert problems.by_fingerprint(conn, "f1") == problem
    assert problems.by_fingerprint(conn, "missing") is None
    problem.count, problem.status = 2, "ongoing"
    clock.advance(timedelta(hours=1))
    problems.save(conn, problem, clock)
    assert problems.find(conn, status="ongoing") == [problem]
    created, updated = conn.execute("SELECT created_at, updated_at FROM problems").fetchone()
    assert (created, updated) == ("2026-10-07T09:30:00Z", "2026-10-07T10:30:00Z")


def test_occurrences_by_problem_and_time(conn: sqlite3.Connection, clock: FixedClock) -> None:
    problems.save(conn, sample(clock), clock)
    first = occurrences.add(conn, Occurrence("P-0001", clock.now(), "collect.platform_errors",
                                             run="R-20261007T093000Z-collect", commit="abc1234"), clock)
    clock.advance(timedelta(days=1))
    second = occurrences.add(conn, Occurrence("P-0001", clock.now(), "collect.platform_errors",
                                              evidence={"count": 3}), clock)
    assert second == first + 1
    assert [item.id for item in occurrences.find(conn, "P-0001")] == [first, second]
    later = occurrences.find(conn, "P-0001", since=clock.now())
    assert [(item.id, item.evidence) for item in later] == [(second, {"count": 3})]
    assert occurrences.get(conn, first).commit == "abc1234"  # type: ignore[union-attr]
