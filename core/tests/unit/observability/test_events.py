import json
from datetime import datetime, timedelta, timezone

import pytest

from tightrein.observability import events
from tightrein.observability.events import Event, EventLog
from tightrein.observability.redact import REDACTED, Redactor
from tightrein.store.files.layout import WorkspaceLayout

AT = datetime(2026, 10, 5, 3, 0, 5, tzinfo=timezone.utc)
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN = "00f067aa0ba902b7"
PARENT = "a3ce929d0e0e4736"


def gate(**fields):
    base = dict(timestamp=AT, run_id="R-20261005-030000-aggregate", trace_id=TRACE, span_id=SPAN,
                parent_span_id=PARENT, stage="aggregate", operation="gate", decision="执行", reason="有待聚合的信号")
    return Event(**{**base, **fields})


@pytest.fixture
def layout(tmp_path):
    return WorkspaceLayout(tmp_path / "workspaces" / "sample")


def test_event_serializes_all_fields_in_order():
    data = gate(attributes={"query": "越权"}, score={"result": "pass"}).to_dict()
    assert list(data) == [
        "timestamp", "run_id", "trace_id", "span_id", "parent_span_id", "stage", "operation", "agent", "model",
        "input_tokens", "output_tokens", "cost_usd", "duration_ms", "status", "error_type", "decision", "reason",
        "score", "artifact", "attributes",
    ]
    assert data["timestamp"] == "2026-10-05T03:00:05Z"
    assert (data["attributes"], data["score"]) == ({"query": "越权"}, {"result": "pass"})
    assert "\n" not in events.encode(gate(reason="第一行\n第二行"))


@pytest.mark.parametrize("fields, message", [
    ({"operation": "deploy"}, "operation 只能是"),
    ({"trace_id": "xyz"}, "trace_id 须为 32 位十六进制"),
    ({"trace_id": "0" * 32}, "trace_id 须为 32 位十六进制"),
    ({"span_id": "ABCDEF0123456789"}, "span_id 须为 16 位十六进制"),
    ({"parent_span_id": "0" * 16}, "parent_span_id 须为 16 位十六进制"),
    ({"timestamp": datetime(2026, 10, 5, 3, 0)}, "事件时间需要带时区"),
])
def test_invalid_events(fields, message):
    with pytest.raises(ValueError, match=message):
        gate(**fields)


def test_events_are_appended_to_the_daily_file(layout):
    log = EventLog(layout, Redactor())
    assert log.write(gate())
    assert log.write(gate(timestamp=AT + timedelta(minutes=1), decision="跳过"))
    assert log.write(gate(timestamp=AT + timedelta(days=1)))
    first = layout.logs_dir() / "events-2026-10-05.jsonl"
    assert log.path_for(gate()) == first
    assert [event.decision for event in events.read(first)] == ["执行", "跳过"]
    assert events.read(layout.logs_dir() / "events-2026-10-06.jsonl") == (gate(timestamp=AT + timedelta(days=1)),)
    assert log.failures == []


def test_the_day_is_taken_in_utc(layout):
    tokyo = timezone(timedelta(hours=9))
    log = EventLog(layout, Redactor())
    assert log.path_for(gate(timestamp=datetime(2026, 10, 6, 8, 0, tzinfo=tokyo))).name == "events-2026-10-05.jsonl"


def test_output_mode_writes_to_the_output_directory(tmp_path):
    layout = WorkspaceLayout(tmp_path / "workspaces" / "sample", output_dir=tmp_path / "out")
    log = EventLog(layout, Redactor())
    assert log.write(gate())
    assert (tmp_path / "out" / "events.jsonl").is_file()
    assert not layout.logs_dir().exists()


def test_events_are_redacted_before_writing(layout):
    redactor = Redactor()
    redactor.register("Pa55-w0rd!")
    log = EventLog(layout, redactor)
    event = gate(
        trace_id="13812345678000000000000000000001",
        reason="登录用 Pa55-w0rd! 失败",
        error_type="password=abc",
        attributes={"query": "手机 13812345678", "token": "x", "hits": ["DP-0012"]},
        score={"item": "evidence-cited", "result": "fail", "method": "code", "note": "引用了 Pa55-w0rd!"},
        input_tokens=13812345678,
    )
    log.write(event)
    line = log.path_for(event).read_text(encoding="utf-8")
    assert "Pa55-w0rd!" not in line and "手机 13812345678" not in line
    written = events.read(log.path_for(event))[0]
    assert written.trace_id == "13812345678000000000000000000001"
    assert written.input_tokens == 13812345678
    assert written.reason == f"登录用 {REDACTED} 失败"
    assert written.error_type == f"password={REDACTED}"
    assert written.attributes == {"query": f"手机 {REDACTED}", "token": REDACTED, "hits": ["DP-0012"]}
    assert written.score == {"item": "evidence-cited", "result": "fail", "method": "code", "note": f"引用了 {REDACTED}"}


def test_write_failures_are_recorded_not_raised(layout):
    layout.data_dir().mkdir(parents=True)
    layout.logs_dir().write_text("不是目录", encoding="utf-8")
    log = EventLog(layout, Redactor())
    assert log.write(gate()) is False
    assert len(log.failures) == 1
    assert log.failures[0].path == layout.logs_dir() / "events-2026-10-05.jsonl"
    assert log.failures[0].reason.startswith(("FileExistsError", "NotADirectoryError"))


def test_read_reports_the_line_number(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(events.encode(gate()) + "\n\n" + json.dumps({"timestamp": "x"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="第 3 行不是合法的事件"):
        events.read(path)
    path.write_text(json.dumps({**gate().to_dict(), "extra": 1}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不认识的字段：extra"):
        events.read(path)
