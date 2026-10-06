from datetime import date, datetime, timedelta, timezone

import pytest

from tightrein.config.routes import Routes
from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import Stage
from tightrein.runner import limits
from tightrein.runner.limits import DailyBudget, RunWatch
from tightrein.runner.result import Usage
from tightrein.runner.task import Limits
from tightrein.runner.transcript import EventDraft
from tightrein.store.migrations.runner import open_database
from tightrein.store.repos import budget_usage

TOKYO = timezone(timedelta(hours=9))
CAPABILITIES = Routes({"gpt": {"tool": "codex", "model": "gpt-5", "inputUsdPerMTok": 1.25, "outputUsdPerMTok": 10}})


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc))


@pytest.fixture
def conn(tmp_path, clock):
    connection = open_database(tmp_path / "tightrein.db", clock)
    yield connection
    connection.close()


def test_stage_settings(make_config):
    config = make_config()
    assert limits.stage_limits(config, Stage.TRIAGE) == Limits(30, 600000, None)
    assert limits.stage_limits(config, Stage.LEARN) == Limits()
    assert (limits.budget_per_day(config, Stage.TRIAGE), limits.budget_per_day(config, Stage.FIX)) == (5.0, None)


def test_cost_is_estimated_when_the_tool_does_not_report_it():
    estimated = limits.with_cost(Usage(200_000, 10_000), CAPABILITIES, "codex", "gpt-5")
    assert (estimated.cost_usd, estimated.cost_estimated) == (pytest.approx(0.35), True)
    reported = Usage(1, 1, None, 0.5, False)
    assert limits.with_cost(reported, CAPABILITIES, "codex", "gpt-5") == reported
    assert limits.with_cost(Usage(1, 1), CAPABILITIES, "codex", None) == Usage(1, 1)
    assert limits.with_cost(Usage(None, None), CAPABILITIES, "codex", "gpt-5") == Usage()


def test_daily_budget_uses_the_local_date(conn, clock):
    budget = DailyBudget(conn, clock, TOKYO)
    budget.add(Stage.TRIAGE, Usage(1000, 200, None, 3.0, True))
    budget.add(Stage.TRIAGE, Usage(500, 100, None, 2.5, False))
    saved = budget_usage.get(conn, Stage.TRIAGE, date(2026, 10, 5))
    assert (saved.cost_usd, saved.input_tokens, saved.output_tokens, saved.estimated) == (5.5, 1500, 300, True)
    assert budget.spent(Stage.TRIAGE) == 5.5
    assert budget.exhausted(Stage.TRIAGE, 5.0)
    assert not budget.exhausted(Stage.TRIAGE, None)
    assert not budget.exhausted(Stage.FIX, 1.0)
    clock.advance(timedelta(hours=1))
    assert budget.spent(Stage.TRIAGE) == 0.0


def test_empty_usage_is_not_recorded(conn, clock):
    DailyBudget(conn, clock, TOKYO).add(Stage.TRIAGE, Usage())
    assert budget_usage.find(conn) == []


def test_turns_are_counted_from_tool_calls():
    watch = RunWatch(max_turns=2, max_cost_usd=None, cost=lambda usage: usage)
    call = EventDraft("tool-call", "assistant", tool_name="shell", tool_call_id="1")
    assert [watch.observe(call), watch.observe(EventDraft("message", "assistant")), watch.observe(call)] == [None] * 3
    assert watch.observe(call) == "turn-limit"
    assert watch.turns == 3


def test_cost_is_accumulated_from_usage_events():
    watch = RunWatch(max_turns=None, max_cost_usd=0.5,
                     cost=lambda usage: limits.with_cost(usage, CAPABILITIES, "codex", "gpt-5"))
    turn = EventDraft("usage", "system", usage=Usage(200_000, 10_000))
    assert watch.observe(turn) is None
    assert watch.observe(turn) == "cost-limit"
    assert watch.usage.cost_usd == pytest.approx(0.7)
    unlimited = RunWatch(max_turns=None, max_cost_usd=None, cost=lambda usage: usage)
    assert unlimited.observe(EventDraft("tool-call", "assistant", tool_name="x", tool_call_id="1")) is None
