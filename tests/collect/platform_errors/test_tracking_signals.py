from datetime import UTC, datetime

from tightrein.collect.common.signals import SignalFactory, SignalLimits
from tightrein.collect.platform_errors import tracking_signals
from tightrein.collect.platform_errors.error_tracking.sentry import Breadcrumb, StackFrame, TrackedIssue
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.raw import RawDir
from tightrein.protocol.security import Redactor

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
RELEASE = "d6f37025a1b2"


def issue(**changes):
    values = {"group": "sentry:acme/4512", "frontend": True, "title": "TypeError: x is undefined", "type": "TypeError",
              "message": "x is undefined", "culprit": "render(app)", "level": "error", "count": 7, "user_count": 3,
              "first_seen": datetime(2026, 10, 4, 1, tzinfo=UTC), "last_seen": datetime(2026, 10, 5, 2, 30, tzinfo=UTC),
              "release": "web@1.2", "environment": "production", "permalink": "https://sentry.io/issues/4512/",
              "frames": (StackFrame("node_modules/react.js", "commit", 9, False),
                         StackFrame("src/app.js", "render", 12, True)),
              "breadcrumbs": (Breadcrumb("2026-10-05T02:29:00Z", "ui.click", "保存"),),
              "url": "https://demo.example.com/orders?token=abc", "browser": "Chrome 129"}
    return TrackedIssue(**{**values, **changes})


def factory(tmp_path):
    return SignalFactory(run="R-20261005T030000Z-collect", source="collect.platform_errors", clock=FixedClock(NOW),
                         redactor=Redactor(), raw=RawDir(tmp_path), limits=SignalLimits(1000, 16384, 2000))


def test_error_tracking_issues_become_signals_with_the_platform_group(tmp_path):
    [signal] = tracking_signals.to_signals([issue()], factory(tmp_path), lambda at: RELEASE, 10)
    assert (signal.check_type, signal.location, signal.symbol, signal.commit) == (
        "error", "src/app.js:render", "render", RELEASE)
    assert (signal.message, signal.group_key, signal.environment) == (
        "TypeError: x is undefined", "sentry:acme/4512", "production")
    evidence = signal.evidence
    assert (evidence["sourceName"], evidence["kind"], evidence["count"], evidence["platformRelease"]) == (
        "error_tracking", "frontend", 7, "web@1.2")
    assert evidence["projectFrames"] == [{"symbol": "render", "file": "src/app.js", "line": 12}]
    assert "abc" not in evidence["url"] and evidence["breadcrumbs"][0]["message"] == "保存"


def test_locations_fall_back_to_culprit_then_title(tmp_path):
    backend = issue(frontend=False, frames=())
    assert tracking_signals.location(backend) == "render(app)"
    assert tracking_signals.location(issue(frames=(), culprit=None)) == "TypeError: x is undefined"
    assert tracking_signals.message(issue(type=None)) == "TypeError: x is undefined"
    assert tracking_signals.message(issue(message=None)) == "TypeError"
    [signal] = tracking_signals.to_signals([backend], factory(tmp_path), lambda at: None, 10)
    assert "breadcrumbs" not in signal.evidence and signal.evidence["kind"] == "backend" and signal.symbol is None
