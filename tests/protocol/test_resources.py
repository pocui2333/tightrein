import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tightrein.agents.result import RateLimit
from tightrein.protocol.handoff import Tokens
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.resources import IssueBudget, Quota, Slots
from tightrein.settings.load import Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import ToolLayout

NOW = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)
LATER = "2026-10-07T07:00:00Z"


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


def test_the_reserve_is_kept_for_the_user(conn, clock, settings):
    quota = Quota(conn, clock, settings)
    quota.update([RateLimit("claude", "five_hour", "allowed", 0.69, LATER)])
    assert not quota.reserve_reached()
    quota.update([RateLimit("claude", "five_hour", "warning", 0.7, LATER)])
    assert quota.reserve_reached()
    assert quota.reserve_reasons() == ["claude 的 five_hour 额度已用 70%(留余量门槛 70%)"]


def test_weekly_windows_use_the_weekly_threshold(conn, clock, settings):
    quota = Quota(conn, clock, settings)
    quota.update([RateLimit("claude", "opus", "allowed", 0.59, "2026-10-10T00:00:00Z")])
    assert not quota.reserve_reached()
    quota.update([RateLimit("claude", "weekly", "allowed", 0.6, "2026-10-10T00:00:00Z")])
    assert quota.reserve_reached()


def test_signals_after_their_reset_time_are_ignored(conn, clock, settings):
    quota = Quota(conn, clock, settings)
    quota.update([RateLimit("claude", "five_hour", "rejected", 1.0, LATER)])
    assert quota.halted_until("claude") == datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    clock.advance(timedelta(hours=2))
    assert quota.halted_until("claude") is None
    assert not quota.reserve_reached()
    assert quota.current() == []


def test_exhaustion_halts_only_that_tool(conn, clock, settings):
    quota = Quota(conn, clock, settings)
    quota.update([RateLimit("agy", "weekly", "rejected", 1.0, "2026-10-13T00:00:00Z"),
                  RateLimit("claude", "five_hour", "rejected", 1.0, LATER)])
    assert quota.halted_until("agy") == datetime(2026, 10, 13, tzinfo=UTC)
    assert quota.halted_until("codex") is None
    assert quota.halted_until() == datetime(2026, 10, 13, tzinfo=UTC)
    quota.update([RateLimit("claude", "five_hour", "allowed", 0.1, LATER)])
    assert quota.halted_until("claude") is None


def test_an_unknown_reset_time_waits_the_configured_time(conn, clock, settings):
    quota = Quota(conn, clock, settings)
    quota.update([RateLimit("codex", "five_hour", "rejected", 1.0, None)])
    assert quota.halted_until("codex") == NOW + timedelta(hours=1)
    clock.advance(timedelta(hours=1))
    assert quota.halted_until("codex") is None


def test_quota_survives_a_new_connection_object(conn, clock, settings):
    Quota(conn, clock, settings).update([RateLimit("claude", "five_hour", "rejected", 1.0, LATER)])
    assert Quota(conn, clock, settings).halted_until("claude") is not None
    Quota(conn, clock, settings).update([])


def test_issue_budget_counts_cache_reads_at_a_tenth(conn, clock, settings):
    budget = IssueBudget(conn, clock, settings)
    # 输入 1.2M 中 1M 是缓存读取：计 0.2M + 0.1M + 输出 0.05M
    assert budget.add("0018", Tokens(input=1_200_000, output=50_000, cache_read=1_000_000)) == 350_000
    assert budget.used("0018") == 350_000 and budget.used("0019") == 0
    assert budget.remaining("0018") == 1_650_000
    assert not budget.exceeded("0018")
    budget.add("0018", Tokens(input=1_650_000))
    assert budget.exceeded("0018")
    assert not budget.exceeded("0019")


def test_slots_limit_concurrent_model_calls(settings):
    slots = Slots(settings)
    finished = threading.Event()

    def fifth() -> None:
        with slots.hold("modelCalls"):
            finished.set()

    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(slots.hold("modelCalls"))
        thread = threading.Thread(target=fifth)
        thread.start()
        assert not finished.wait(0.1)
    assert finished.wait(2)
    thread.join()
    with pytest.raises(KeyError), slots.hold("platformRps"):
        pass
