import subprocess
from datetime import date, datetime, timedelta, timezone

import pytest

from tightrein.config.user import NOTIFY_MACOS, NOTIFY_NONE
from tightrein.domain.clock import FixedClock
from tightrein.observability import notify
from tightrein.observability.notify import DISABLED, DUPLICATE, FAILED, SENT, Notifier, NotifyResult
from tightrein.observability.redact import REDACTED, Redactor
from tightrein.store import idempotency
from tightrein.store.migrations.runner import open_database

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
TOKYO = timezone(timedelta(hours=9))
KEY = "notify:p0-problem:P-0042:2026-10-05"


class FakeOsascript:
    def __init__(self, returncode=0, stderr="", error=None):
        self.returncode = returncode
        self.stderr = stderr
        self.error = error
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def conn(tmp_path, clock):
    connection = open_database(tmp_path / "data" / "tightrein.db", clock)
    yield connection
    connection.close()


def notifier(conn, clock, run, method=NOTIFY_MACOS, redactor=None, zone=TOKYO):
    return Notifier(conn, method, clock, redactor or Redactor(), run, zone)


def test_the_key_is_event_type_subject_and_date():
    assert notify.notification_key("p0-problem", "P-0042", date(2026, 10, 5)) == KEY
    for event_type, subject in (("", "P-0042"), ("a:b", "P-0042"), ("run", "")):
        with pytest.raises(ValueError):
            notify.notification_key(event_type, subject, date(2026, 10, 5))


def test_macos_notification_uses_osascript(conn, clock):
    osascript = FakeOsascript()
    result = notifier(conn, clock, osascript).notify("p0-problem", "P-0042", '订单接口 "越权" 返回 200', "tightrein：sample")
    assert result == NotifyResult(SENT, KEY)
    assert osascript.calls == [[
        "osascript", "-e", 'display notification "订单接口 \\"越权\\" 返回 200" with title "tightrein：sample"',
    ]]
    assert idempotency.get(conn, KEY).status == idempotency.DONE


def test_the_same_event_is_notified_once_a_day(conn, clock):
    osascript = FakeOsascript()
    sender = notifier(conn, clock, osascript)
    assert sender.notify("p0-problem", "P-0042", "第一次").status == SENT
    assert sender.notify("p0-problem", "P-0042", "第二次") == NotifyResult(DUPLICATE, KEY)
    assert sender.notify("p0-problem", "P-0043", "另一个对象").status == SENT
    assert sender.notify("deploy-failed", "P-0042", "另一种事件").status == SENT
    clock.advance(timedelta(days=1))
    assert sender.notify("p0-problem", "P-0042", "第二天").status == SENT
    assert len(osascript.calls) == 4


def test_the_date_of_the_key_is_the_local_date(conn):
    clock = FixedClock(datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc))
    result = notifier(conn, clock, FakeOsascript()).notify("p0-problem", "P-0042", "正文")
    assert result.key == "notify:p0-problem:P-0042:2026-10-06"
    new_york = timezone(timedelta(hours=-4))
    other = notifier(conn, clock, FakeOsascript(), zone=new_york).notify("p0-problem", "P-0042", "正文")
    assert other.key == "notify:p0-problem:P-0042:2026-10-05"


def test_one_local_day_spans_the_utc_midnight(conn):
    clock = FixedClock(datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc))
    osascript = FakeOsascript()
    sender = notifier(conn, clock, osascript)
    assert sender.notify("p0-problem", "P-0042", "早上").status == SENT
    clock.advance(timedelta(hours=6))
    assert sender.notify("p0-problem", "P-0042", "同一天下午").status == DUPLICATE
    clock.advance(timedelta(hours=13))
    assert sender.notify("p0-problem", "P-0042", "第二天").status == SENT
    assert len(osascript.calls) == 2


def test_method_none_never_calls_the_command(conn, clock):
    osascript = FakeOsascript()
    result = notifier(conn, clock, osascript, method=NOTIFY_NONE).notify("p0-problem", "P-0042", "正文")
    assert result == NotifyResult(DISABLED)
    assert osascript.calls == []
    assert idempotency.get(conn, KEY) is None


def test_a_failed_notification_is_reported_and_can_be_retried(conn, clock):
    broken = FakeOsascript(returncode=1, stderr="execution error: password=abc (-1728)\n")
    result = notifier(conn, clock, broken).notify("p0-problem", "P-0042", "正文")
    assert (result.status, result.key) == (FAILED, KEY)
    assert result.reason == f"osascript 退出码 1：execution error: password={REDACTED} (-1728)"
    assert idempotency.get(conn, KEY) is None
    assert notifier(conn, clock, FakeOsascript()).notify("p0-problem", "P-0042", "正文").status == SENT


@pytest.mark.parametrize("error, reason", [
    (FileNotFoundError(2, "No such file or directory"), "无法执行 osascript：FileNotFoundError"),
    (subprocess.TimeoutExpired(["osascript"], 10), "无法执行 osascript：TimeoutExpired"),
])
def test_command_errors_do_not_raise(conn, clock, error, reason):
    result = notifier(conn, clock, FakeOsascript(error=error)).notify("run", "sample", "正文")
    assert result.status == FAILED
    assert result.reason.startswith(reason)


def test_an_interrupted_notification_is_not_repeated(conn, clock):
    idempotency.begin(conn, KEY, clock)
    osascript = FakeOsascript()
    result = notifier(conn, clock, osascript).notify("p0-problem", "P-0042", "正文")
    assert result.status == DUPLICATE
    assert "中断" in result.reason
    assert osascript.calls == []


def test_database_errors_do_not_raise(conn, clock):
    conn.close()
    result = notifier(conn, clock, FakeOsascript()).notify("p0-problem", "P-0042", "正文")
    assert result.status == FAILED
    assert result.reason.startswith("幂等键读写失败：ProgrammingError")


def test_text_is_redacted_before_sending(conn, clock):
    redactor = Redactor()
    redactor.register("Pa55-w0rd!")
    osascript = FakeOsascript()
    notifier(conn, clock, osascript, redactor=redactor).notify("login-failed", "Company", "密码 Pa55-w0rd! 已失效")
    assert "Pa55-w0rd!" not in osascript.calls[0][2]
    assert REDACTED in osascript.calls[0][2]


def test_unknown_method_is_rejected(conn, clock):
    with pytest.raises(ValueError, match="notify.method"):
        notifier(conn, clock, FakeOsascript(), method="slack")


def test_backslashes_are_escaped_for_applescript():
    assert notify.applescript_string('C:\\logs "x"') == '"C:\\\\logs \\"x\\""'
