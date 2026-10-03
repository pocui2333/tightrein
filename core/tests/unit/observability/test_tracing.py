from datetime import datetime, timedelta, timezone

import pytest

from tightrein.domain.clock import FixedClock
from tightrein.observability import events
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import (
    ENV_PARENT_SPAN_ID,
    ENV_RUN_ID,
    ENV_TRACE_ID,
    Tracer,
    new_span_id,
    new_trace_id,
)
from tightrein.store.files.layout import WorkspaceLayout

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
RUN = "R-20261005-030000-triage"


class Counter:
    """确定的随机源：依次返回 1、2、3……填满所需字节数。"""

    def __init__(self):
        self.value = 0

    def __call__(self, size):
        self.value += 1
        return self.value.to_bytes(size, "big")


class Monotonic:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def monotonic():
    return Monotonic()


@pytest.fixture
def log(tmp_path):
    return EventLog(WorkspaceLayout(tmp_path / "workspaces" / "sample"), Redactor())


@pytest.fixture
def tracer(log, clock, monotonic):
    return Tracer(log, clock, run_id=RUN, stage="triage", monotonic=monotonic, random=Counter())


def written(log):
    return events.read(log.layout.events_log(NOW.date()))


def test_ids_have_the_opentelemetry_format():
    assert len(new_trace_id()) == 32 and len(new_span_id()) == 16


def test_a_span_is_timed_and_written_when_it_ends(tracer, log, clock, monotonic):
    span = tracer.start_span("invoke_agent", agent="claim-verifier", model="claude-opus")
    assert tracer.current() is span
    clock.advance(timedelta(seconds=3))
    monotonic.value += 2.5
    span.set(input_tokens=1200, output_tokens=300, attributes={"subject": "P-0042"})
    event = tracer.end_span(span, cost_usd=0.04)
    assert tracer.current() is None
    assert (event.timestamp, event.duration_ms, event.status) == (NOW, 2500, "ok")
    assert (event.run_id, event.stage, event.trace_id) == (RUN, "triage", "00000000000000000000000000000001")
    assert (event.span_id, event.parent_span_id) == ("0000000000000002", None)
    assert (event.agent, event.input_tokens, event.cost_usd) == ("claim-verifier", 1200, 0.04)
    assert event.attributes == {"subject": "P-0042"}
    assert written(log) == (event,)


def test_nested_spans_record_their_parent(tracer, log, monotonic):
    with tracer.span("run_script", stage="fix", attributes={"command": "git status"}) as outer:
        monotonic.value += 1
        with tracer.span("execute_tool") as inner:
            monotonic.value += 0.25
            gate = tracer.event("gate", decision="执行", reason="有待分诊的问题")
        with tracer.span("invoke_agent") as sibling:
            pass
    rows = {row.span_id: row for row in written(log)}
    assert rows[inner.span_id].parent_span_id == outer.span_id
    assert rows[sibling.span_id].parent_span_id == outer.span_id
    assert rows[gate.span_id].parent_span_id == inner.span_id
    assert rows[outer.span_id].parent_span_id is None
    assert rows[outer.span_id].stage == "fix"
    assert rows[inner.span_id].stage == "triage"
    assert (rows[outer.span_id].duration_ms, rows[inner.span_id].duration_ms) == (1250, 250)
    assert rows[gate.span_id].duration_ms is None
    assert [row.span_id for row in written(log)] == [gate.span_id, inner.span_id, sibling.span_id, outer.span_id]


def test_an_exception_marks_the_span_as_error_and_propagates(tracer, log):
    with pytest.raises(KeyError):
        with tracer.span("run_script"):
            raise KeyError("x")
    (event,) = written(log)
    assert (event.status, event.error_type) == ("error", "KeyError")
    assert tracer.current() is None


def test_status_set_inside_the_span_is_kept(tracer, log):
    with tracer.span("invoke_agent") as span:
        span.set(status="limit-reached", error_type="timeout")
    assert (written(log)[0].status, written(log)[0].error_type) == ("limit-reached", "timeout")


def test_spans_must_end_in_reverse_order(tracer):
    outer = tracer.start_span("run_script")
    inner = tracer.start_span("run_script")
    with pytest.raises(ValueError, match="须按开始的相反顺序结束"):
        tracer.end_span(outer)
    tracer.end_span(inner)
    tracer.end_span(outer)
    with pytest.raises(ValueError, match="已经结束"):
        tracer.end_span(outer)


def test_operations_and_fields_are_checked(tracer):
    with pytest.raises(ValueError, match="span 的 operation"):
        tracer.start_span("gate")
    with pytest.raises(ValueError, match="瞬时事件的 operation"):
        tracer.event("invoke_agent")
    with pytest.raises(ValueError, match="trace_id"):
        tracer.event("gate", trace_id="x")
    with pytest.raises(ValueError, match="colour"):
        tracer.start_span("run_script", colour="red")


def test_child_environment_and_resuming_from_it(tracer, log, clock):
    assert tracer.child_environment() == {ENV_TRACE_ID: tracer.trace_id, ENV_RUN_ID: RUN}
    with tracer.span("invoke_agent") as span:
        environment = tracer.child_environment()
    assert environment == {ENV_TRACE_ID: tracer.trace_id, ENV_RUN_ID: RUN, ENV_PARENT_SPAN_ID: span.span_id}
    child = Tracer.from_environment(log, clock, {**environment, "PATH": "/usr/bin"}, stage="triage")
    event = child.event("user_action", decision="kb search")
    assert (event.trace_id, event.parent_span_id, event.run_id) == (tracer.trace_id, span.span_id, RUN)


def test_without_environment_a_new_trace_starts(log, clock):
    tracer = Tracer.from_environment(log, clock, {}, random=Counter())
    assert (tracer.trace_id, tracer.run_id, tracer.parent_span_id) == ("0" * 31 + "1", None, None)


@pytest.mark.parametrize("environment, name", [
    ({ENV_TRACE_ID: "not-a-trace"}, ENV_TRACE_ID),
    ({ENV_TRACE_ID: "a" * 32, ENV_PARENT_SPAN_ID: "short"}, ENV_PARENT_SPAN_ID),
])
def test_invalid_environment_is_rejected(log, clock, environment, name):
    with pytest.raises(ValueError, match=name):
        Tracer.from_environment(log, clock, environment)


def test_write_failures_do_not_break_the_span(tmp_path, clock):
    layout = WorkspaceLayout(tmp_path / "workspaces" / "sample")
    layout.data_dir().mkdir(parents=True)
    layout.logs_dir().write_text("", encoding="utf-8")
    log = EventLog(layout, Redactor())
    tracer = Tracer(log, clock, run_id=RUN)
    with tracer.span("run_script"):
        pass
    assert len(log.failures) == 1
