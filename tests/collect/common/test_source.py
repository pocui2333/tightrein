import threading

import pytest

from tightrein.collect.common.source import (
    SourceInvalid,
    SourceMisconfigured,
    SourceResult,
    SourceStatus,
    SourceUnavailable,
    each,
    failed,
    guarded,
    skipped,
)
from tightrein.protocol.handoff import Metrics


def test_result_status_and_reasons():
    with pytest.raises(ValueError):
        SourceResult("collect.alerts", "running", [], 0, None, None, {}, Metrics())
    with pytest.raises(ValueError, match="原因"):
        SourceResult("collect.alerts", SourceStatus.SKIPPED, [], 0, None, None, {}, Metrics())
    with pytest.raises(ValueError, match="原因"):
        SourceResult("collect.alerts", SourceStatus.PARTIAL, [], 0, None, "", {}, Metrics())
    with pytest.raises(ValueError, match="读取位置"):
        SourceResult("collect.alerts", SourceStatus.FAILED, [], 0, None, "坏了", {"k": 1}, Metrics())
    assert skipped("collect.alerts", "没有到点").reason == "没有到点"
    assert failed("collect.alerts", "平台 503").status is SourceStatus.FAILED


def test_guarded_classifies_typed_errors_and_records_the_duration():
    ticks = iter([10.0, 10.25])

    def broken() -> SourceResult:
        raise SourceUnavailable("Sentry 返回 503")

    result = guarded("collect.platform_errors", broken, monotonic=lambda: next(ticks))
    assert (result.status, result.reason) == (SourceStatus.FAILED, "unavailable：Sentry 返回 503")
    assert result.metrics.duration_ms == 250 and result.state == {}
    assert result.metrics.produced == {"signals": 0}


def test_guarded_stops_waiting_for_a_stuck_source():
    release = threading.Event()

    def stuck() -> SourceResult:
        release.wait(5)
        return SourceResult("collect.alerts", SourceStatus.DONE, [], 0, None, None, {"k": "moved"}, Metrics())

    result = guarded("collect.alerts", stuck, timeout_s=0.05)
    release.set()
    assert result.status is SourceStatus.FAILED and result.reason.startswith("timeout：") and result.state == {}


def test_guarded_lets_programming_errors_through():
    def buggy() -> SourceResult:
        raise KeyError("oops")

    with pytest.raises(KeyError):
        guarded("collect.alerts", buggy)


def test_each_runs_parts_independently():
    def bad() -> int:
        raise SourceInvalid("不是 JSON")

    found = each({"a": lambda: 1, "b": bad, "c": lambda: 3}, workers=2)
    assert (found["a"], found["c"]) == (1, 3)
    assert isinstance(found["b"], SourceInvalid)
    assert each({}, workers=4) == {}
    with pytest.raises(ZeroDivisionError):
        each({"x": lambda: 1 / 0}, workers=1)
    assert isinstance(each({"m": lambda: (_ for _ in ()).throw(SourceMisconfigured("缺参数"))}, workers=1)["m"],
                      SourceMisconfigured)
