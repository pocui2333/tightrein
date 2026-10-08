import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tightrein.agents.result import CallStatus
from tightrein.protocol.limits import Action, Breaker, backoff_s, decide, no_progress
from tightrein.protocol.naming import FixedClock
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import ToolLayout

NOW = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings.from_data(json.loads(ToolLayout.discover().defaults.read_text(encoding="utf-8")))


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def conn(tmp_path: Path, clock: FixedClock) -> Iterator[sqlite3.Connection]:
    connection = open_database(tmp_path / "tightrein.db", clock=clock)
    yield connection
    connection.close()


def test_transient_errors_are_retried_twice(settings):
    assert [decide(CallStatus.TRANSIENT, attempt, fallback_used=False, settings=settings) for attempt in (1, 2, 3)] == [
        Action.RETRY, Action.RETRY, Action.STOP]


def test_schema_invalid_resumes_the_session_once(settings):
    assert decide(CallStatus.SCHEMA_INVALID, 1, fallback_used=False, settings=settings) is Action.RESUME_WITH_REASON
    assert decide(CallStatus.SCHEMA_INVALID, 2, fallback_used=False, settings=settings) is Action.STOP


@pytest.mark.parametrize("status", [CallStatus.REFUSED, CallStatus.UNAVAILABLE])
def test_refused_or_unavailable_switch_to_the_fallback_once(settings, status):
    assert decide(status, 1, fallback_used=False, settings=settings) is Action.FALLBACK
    assert decide(status, 1, fallback_used=True, settings=settings) is Action.STOP


def test_quota_exhaustion_halts_everything(settings):
    assert decide(CallStatus.QUOTA_EXHAUSTED, 1, fallback_used=False, settings=settings) is Action.HALT_ALL


@pytest.mark.parametrize("status", [CallStatus.TIMEOUT, CallStatus.TURN_LIMIT, CallStatus.BUDGET_LIMIT,
                                    CallStatus.AUTH_FAILED, CallStatus.BOUNDARY, CallStatus.FAILED])
def test_limits_auth_and_other_failures_stop(settings, status):
    assert decide(status, 1, fallback_used=False, settings=settings) is Action.STOP


def test_backoff_uses_full_jitter_with_caps(settings):
    def top(attempt, overloaded=False):
        return backoff_s(attempt, retry_after_s=None, overloaded=overloaded, settings=settings, random=lambda: 1.0)

    assert [top(1), top(2), top(3), top(6)] == [1.0, 2.0, 4.0, 20.0]
    assert top(7, overloaded=True) == 60.0
    assert backoff_s(3, retry_after_s=None, overloaded=False, settings=settings, random=lambda: 0.25) == 1.0
    assert backoff_s(1, retry_after_s=0.0, overloaded=False, settings=settings, random=lambda: 0.5) == 0.0


def test_retry_after_is_followed(settings):
    assert backoff_s(1, retry_after_s=37.0, overloaded=True, settings=settings, random=lambda: 0.1) == 37.0


def test_dependency_breaker_opens_after_five_failures_and_probes_after_the_pause(conn, clock, settings):
    breaker = Breaker(conn, clock, settings)
    for _ in range(4):
        breaker.record("claude", False)
    assert breaker.allow("claude")
    breaker.record("claude", False)
    assert not breaker.allow("claude")
    assert breaker.allow("agy")
    clock.advance(timedelta(seconds=59))
    assert not breaker.allow("claude")
    clock.advance(timedelta(seconds=1))
    assert breaker.allow("claude")  # 一次试探
    assert not breaker.allow("claude")  # 试探结束前其余调用仍被挡住
    breaker.record("claude", False)
    clock.advance(timedelta(seconds=30))
    assert not breaker.allow("claude")
    clock.advance(timedelta(seconds=30))
    assert breaker.allow("claude")
    breaker.record("claude", True)
    assert breaker.allow("claude") and breaker.allow("claude")


def test_a_success_resets_the_consecutive_count(conn, clock, settings):
    breaker = Breaker(conn, clock, settings)
    for _ in range(4):
        breaker.record("codex", False)
    breaker.record("codex", True)
    for _ in range(4):
        breaker.record("codex", False)
    assert breaker.allow("codex")


def test_authentication_failures_trip_the_breaker_at_once(conn, clock, settings):
    breaker = Breaker(conn, clock, settings)
    breaker.trip("agy")
    assert not breaker.allow("agy")
    clock.advance(timedelta(seconds=60))
    assert breaker.allow("agy")


def test_the_breaker_state_is_shared_through_the_database(conn, clock, settings):
    Breaker(conn, clock, settings).trip("claude")
    assert not Breaker(conn, clock, settings).allow("claude")


def test_object_breaker_after_three_failures(conn, clock, settings):
    breaker = Breaker(conn, clock, settings)
    assert [breaker.object_failed("0018") for _ in range(3)] == [False, False, True]
    breaker.object_progressed("0018")
    assert not breaker.object_failed("0018")
    assert not breaker.object_failed("0019")


def test_no_progress_compares_blockers_and_diffs():
    blockers = [("src/a.py:10", "missing_test"), ("src/b.py:3", "wrong_fix")]
    assert no_progress(blockers, list(reversed(blockers)), "d1", "d2")
    assert not no_progress(blockers, blockers[:1], "d1", "d2")
    assert no_progress([], [("src/a.py:1", "x")], "same", "same")
    assert not no_progress([], [], None, None)
    assert not no_progress([("a", "b")], [], "d1", "d2")
    assert not no_progress([], [("a", "b")], None, "d1")
