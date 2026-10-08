from datetime import UTC, datetime

from tightrein.collect.common.signals import SignalFactory, SignalLimits
from tightrein.collect.platform_errors import log_signals, select
from tightrein.collect.platform_errors.log_parse.entries import ExceptionInfo, Frame, LogEntry
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)


def entry(level="error", **changes):
    values = {"stream": '{app="api"}', "position": 0, "occurred_at": datetime(2026, 10, 5, 2, 40, tzinfo=UTC),
              "local_time": None, "level": level, "raw_level": level.upper(), "category": "orders", "event_id": None,
              "message": "查询失败", "exception": ExceptionInfo("KeyError", "'id'"), "raw": '{"msg": "查询失败"}'}
    return LogEntry(**{**values, **changes})


def factory(tmp_path):
    return SignalFactory(run="R-20261005T030000Z-collect", source="collect.platform_errors", clock=FixedClock(NOW),
                         redactor=Redactor(), raw=RawDir(tmp_path), limits=SignalLimits(1000, 16384, 10))


def test_entries_are_filtered_by_level_and_keep_the_first_project_frames():
    frames = (Frame("System.Runtime.Throw", None, None, False),
              Frame("Acme.Orders.OrderService.Load", "Orders.cs", 8, True),
              Frame("Acme.Orders.Controller.Get", "Controller.cs", 3, True))
    chosen = select.apply([entry(frames=frames), entry("information", exception=None)], ["error", "critical"], 1)
    assert len(chosen) == 1
    assert [frame.symbol for frame in chosen[0].project_frames] == ["Acme.Orders.OrderService.Load"]


def test_log_entries_become_signals(tmp_path):
    frames = (Frame("Acme.Orders.OrderService.Load", "Orders.cs", 8, True),)
    with_frames, plain = select.apply([entry(frames=frames), entry(exception=None, raw="x" * 50)], ["error"], 10)
    first, second = log_signals.to_signals([with_frames, plain], factory(tmp_path), lambda at: "c1")
    assert (first.check_type, first.location, first.symbol, first.message) == (
        "error", "Orders.cs:OrderService.Load", "OrderService.Load", "KeyError: 'id'")
    assert first.evidence["projectFrames"] == [{"symbol": "OrderService.Load", "file": "Orders.cs", "line": 8}]
    assert first.evidence["sourceName"] == "log_platform" and first.group_key is None and first.commit == "c1"
    assert (second.location, second.message, second.evidence["excerpt"]) == ("orders", "查询失败", "x" * 10)
    assert log_signals.location(select.SelectedEntry(entry(category=None), ())) == log_signals.NO_CATEGORY
    assert log_signals.short_symbol("Load") == "Load"
