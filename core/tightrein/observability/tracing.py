"""trace 与 span 的上下文管理(architecture/01 6.1、6.2 与 5.5)。

- 一次运行是一个 trace；每个环节、每次 agent 调用、每次工具执行是一个 span，结束时写一行事件：timestamp 为开始时间，
  duration_ms 由单调时钟计算，不受系统时间调整影响。
- 进行中的 span 保存在 Tracer 的栈中，嵌套的 span 以外层 span 为 parent_span_id；最外层以 Tracer 的 parent_span_id 为父。
  agent 子进程经环境变量继承运行编号、trace 与父 span(from_environment、child_environment)，其中的 `kb` 调用挂到同一 trace 下。
- 关卡判定(gate)与用户操作(user_action)是瞬时事件，以当前 span 为父写一行，没有耗时。
- span 中抛出异常时状态记为 error、error_type 为异常类名，异常照常抛出；span 须按开始的相反顺序结束。
编号由注入的随机源生成，测试中可以得到确定的编号。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
from datetime import datetime
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.observability.events import (
    INSTANT_OPERATIONS,
    SPAN_OPERATIONS,
    Event,
    EventLog,
    is_span_id,
    is_trace_id,
)

ENV_RUN_ID = "TIGHTREIN_RUN_ID"
ENV_TRACE_ID = "TIGHTREIN_TRACE_ID"
ENV_PARENT_SPAN_ID = "TIGHTREIN_PARENT_SPAN_ID"
STATUS_OK = "ok"
STATUS_ERROR = "error"
TRACE_ID_BYTES = 16
SPAN_ID_BYTES = 8
MILLISECONDS_PER_SECOND = 1000

RandomBytes = Callable[[int], bytes]

_STRUCTURAL = frozenset({"timestamp", "run_id", "trace_id", "span_id", "parent_span_id", "operation", "duration_ms"})
SETTABLE_FIELDS = frozenset(item.name for item in dataclass_fields(Event)) - _STRUCTURAL


def new_trace_id(random: RandomBytes = os.urandom) -> str:
    return random(TRACE_ID_BYTES).hex()


def new_span_id(random: RandomBytes = os.urandom) -> str:
    return random(SPAN_ID_BYTES).hex()


def _check_fields(values: Mapping[str, Any]) -> None:
    unknown = sorted(set(values) - SETTABLE_FIELDS)
    if unknown:
        raise ValueError(f"事件没有这些字段或不能由调用方设置：{', '.join(unknown)}")


class Span:
    """进行中的 span；set 补充用量、决定等字段，attributes 按键合并。"""

    def __init__(
        self, operation: str, span_id: str, parent_span_id: str | None, started_at: datetime, started: float,
        values: dict[str, Any],
    ) -> None:
        self.operation = operation
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.started_at = started_at
        self.started = started
        self.values: dict[str, Any] = {"attributes": {}}
        self.ended = False
        self.set(**values)

    def set(self, **values: Any) -> None:
        _check_fields(values)
        attributes = values.pop("attributes", None)
        self.values.update(values)
        if attributes:
            self.values["attributes"] = {**self.values["attributes"], **attributes}


class Tracer:
    def __init__(
        self,
        log: EventLog,
        clock: Clock,
        *,
        run_id: str | None,
        stage: str | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        random: RandomBytes = os.urandom,
    ) -> None:
        if trace_id is not None and not is_trace_id(trace_id):
            raise ValueError(f"trace_id 须为 32 位十六进制：{trace_id}")
        if parent_span_id is not None and not is_span_id(parent_span_id):
            raise ValueError(f"parent_span_id 须为 16 位十六进制：{parent_span_id}")
        self.log = log
        self.clock = clock
        self.run_id = run_id
        self.stage = stage
        self.random = random
        self.trace_id = trace_id or new_trace_id(random)
        self.parent_span_id = parent_span_id
        self.monotonic = monotonic
        self._stack: list[Span] = []

    @classmethod
    def from_environment(
        cls, log: EventLog, clock: Clock, environ: Mapping[str, str], *, stage: str | None = None, **options: Any
    ) -> Tracer:
        """由执行器传给 agent 进程的环境变量接续 trace；变量不存在时开始新的 trace。"""
        trace_id = environ.get(ENV_TRACE_ID) or None
        parent = environ.get(ENV_PARENT_SPAN_ID) or None
        if trace_id is not None and not is_trace_id(trace_id):
            raise ValueError(f"环境变量 {ENV_TRACE_ID} 不是合法的 trace 编号：{trace_id}")
        if parent is not None and not is_span_id(parent):
            raise ValueError(f"环境变量 {ENV_PARENT_SPAN_ID} 不是合法的 span 编号：{parent}")
        run_id = environ.get(ENV_RUN_ID) or None
        return cls(log, clock, run_id=run_id, stage=stage, trace_id=trace_id, parent_span_id=parent, **options)

    def current(self) -> Span | None:
        return self._stack[-1] if self._stack else None

    def _parent(self) -> str | None:
        current = self.current()
        return current.span_id if current is not None else self.parent_span_id

    def child_environment(self) -> dict[str, str]:
        """传给子进程的环境变量：子进程中的事件以当前 span 为父。"""
        environment = {ENV_TRACE_ID: self.trace_id}
        if self.run_id is not None:
            environment[ENV_RUN_ID] = self.run_id
        parent = self._parent()
        if parent is not None:
            environment[ENV_PARENT_SPAN_ID] = parent
        return environment

    def start_span(self, operation: str, **values: Any) -> Span:
        if operation not in SPAN_OPERATIONS:
            raise ValueError(f"span 的 operation 只能是 {', '.join(SPAN_OPERATIONS)}：{operation}")
        span = Span(operation, new_span_id(self.random), self._parent(), self.clock.now(), self.monotonic(), values)
        self._stack.append(span)
        return span

    def end_span(self, span: Span, status: str | None = None, **values: Any) -> Event:
        """结束 span 并写一行事件；status 缺省时取 span 中已设置的状态，都没有时为 ok。"""
        if span.ended:
            raise ValueError(f"span {span.span_id} 已经结束")
        if self.current() is not span:
            raise ValueError(f"span {span.span_id} 不是当前 span，须按开始的相反顺序结束")
        span.set(**values)
        span.values["status"] = status or span.values.get("status") or STATUS_OK
        duration_ms = round((self.monotonic() - span.started) * MILLISECONDS_PER_SECOND)
        self._stack.pop()
        span.ended = True
        return self._write(span.operation, span.span_id, span.parent_span_id, span.started_at, duration_ms, span.values)

    @contextmanager
    def span(self, operation: str, **values: Any) -> Iterator[Span]:
        span = self.start_span(operation, **values)
        try:
            yield span
        except BaseException as error:
            self.end_span(span, STATUS_ERROR, error_type=type(error).__name__)
            raise
        self.end_span(span)

    def event(self, operation: str, **values: Any) -> Event:
        """瞬时事件(gate、user_action)。"""
        if operation not in INSTANT_OPERATIONS:
            raise ValueError(f"瞬时事件的 operation 只能是 {', '.join(INSTANT_OPERATIONS)}：{operation}")
        _check_fields(values)
        return self._write(operation, new_span_id(self.random), self._parent(), self.clock.now(), None, values)

    def _write(
        self, operation: str, span_id: str, parent: str | None, at: datetime, duration_ms: int | None,
        values: Mapping[str, Any],
    ) -> Event:
        merged = {"stage": self.stage, **values}
        event = Event(
            timestamp=at, run_id=self.run_id, trace_id=self.trace_id, span_id=span_id, parent_span_id=parent,
            operation=operation, duration_ms=duration_ms, **merged,
        )
        self.log.write(event)
        return event
