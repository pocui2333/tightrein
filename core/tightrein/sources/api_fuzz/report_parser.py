"""解析 Schemathesis 的 NDJSON 事件流(architecture/04 2.5)。字段取法以锁定版本 4.28.0 实际产出的报告为准。

每行是一个 `{"<事件名>": {...}}`：
- ScenarioFinished(以及 FuzzScenarioFinished)的 recorder 中，cases 为用例(方法、路由模板、查询参数、请求体)，
  checks 为每个用例的检查结果，status 为 failure 的带 failure_info.failure(operation、title、message)，
  interactions 为该用例的请求与响应(实际 URL、状态码、耗时秒数、base64 的响应体、记录时间)；
- ScenarioFinished 的 status 不是 skip 时，recorder.label 即已测试的操作；
- EngineFinished 出现才算报告完整；Initialize 给出本次的随机种子；NonFatalError 记为错误说明。
VCR 录制与 JUnit 不解析，保留在原始输出中：事件流中已含失败用例的完整请求与响应。
未知的事件名、无法解析的行、缺少字段的条目跳过并计数。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KNOWN_EVENTS = frozenset({
    "Initialize", "LoadingStarted", "LoadingFinished", "EngineStarted", "PhaseStarted", "PhaseFinished",
    "SchemaAnalysisWarnings", "SuiteStarted", "SuiteFinished", "ScenarioStarted", "ScenarioFinished",
    "FuzzScenarioStarted", "FuzzScenarioFinished", "Interrupted", "NonFatalError", "FatalError", "EngineFinished",
    "RateLimitRetry",
})
SCENARIO_EVENTS = ("ScenarioFinished", "FuzzScenarioFinished")
SKIP_STATUS = "skip"
FAILURE_STATUS = "failure"
MILLISECONDS_PER_SECOND = 1000


@dataclass(frozen=True)
class RecordedCase:
    method: str
    path_template: str
    query: Mapping[str, Any] = field(default_factory=dict)
    body: Any = None
    path_parameters: Mapping[str, Any] = field(default_factory=dict)
    media_type: str | None = None


@dataclass(frozen=True)
class RecordedInteraction:
    method: str
    uri: str
    status: int | None
    elapsed_ms: int | None
    body: bytes
    timestamp: float | None


@dataclass(frozen=True)
class Failure:
    operation: tuple[str, str]
    check: str
    title: str
    message: str
    phase: str
    case_id: str
    case: RecordedCase
    interaction: RecordedInteraction | None


@dataclass(frozen=True)
class RoleReport:
    complete: bool
    tested: tuple[tuple[str, str], ...] = ()
    failures: tuple[Failure, ...] = ()
    unknown: int = 0
    seed: int | None = None
    finished_at: float | None = None
    errors: tuple[str, ...] = ()


def split_label(label: str) -> tuple[str, str] | None:
    method, _, route = label.partition(" ")
    if not method or not route.startswith("/"):
        return None
    return method.upper(), route


def decode_content(content: Any) -> bytes:
    if isinstance(content, Mapping) and "$base64" in content:
        try:
            return base64.b64decode(content["$base64"])
        except (binascii.Error, ValueError):
            return b""
    if isinstance(content, str):
        return content.encode("utf-8")
    return b""


def _case(value: Mapping[str, Any]) -> RecordedCase:
    return RecordedCase(value["method"].upper(), value["path"], value.get("query") or {}, value.get("body"),
                        value.get("path_parameters") or {}, value.get("media_type"))


def _interaction(data: Mapping[str, Any] | None) -> RecordedInteraction | None:
    if not data or "request" not in data:
        return None
    request = data["request"]
    response = data.get("response") or {}
    elapsed = response.get("elapsed")
    return RecordedInteraction(
        request["method"].upper(), request["uri"], response.get("status_code"),
        None if elapsed is None else round(elapsed * MILLISECONDS_PER_SECOND), decode_content(response.get("content")),
        data.get("timestamp"),
    )


class _Collector:
    def __init__(self, error_chars: int) -> None:
        self.error_chars = error_chars
        self.tested: dict[tuple[str, str], None] = {}
        self.failures: list[Failure] = []
        self.unknown = 0
        self.complete = False
        self.seed: int | None = None
        self.finished_at: float | None = None
        self.errors: list[str] = []

    def event(self, name: str, data: Mapping[str, Any]) -> None:
        if name == "Initialize":
            self.seed = data.get("seed")
        elif name == "EngineFinished":
            self.complete = True
            self.finished_at = data.get("timestamp")
        elif name == "NonFatalError":
            value = data.get("value") or {}
            message = str(value.get("message", ""))[:self.error_chars]
            self.errors.append(f"{data.get('label') or '运行'}：{value.get('type', 'Error')}：{message}")
        elif name in SCENARIO_EVENTS:
            self.scenario(name, data)

    def scenario(self, name: str, data: Mapping[str, Any]) -> None:
        recorder = data["recorder"]
        operation = split_label(recorder.get("label") or "")
        if name == "ScenarioFinished" and operation is not None and data.get("status") != SKIP_STATUS:
            self.tested[operation] = None
        cases = recorder.get("cases", {})
        interactions = recorder.get("interactions", {})
        for case_id, checks in recorder.get("checks", {}).items():
            for check in checks:
                if check.get("status") != FAILURE_STATUS:
                    continue
                failure = check["failure_info"]["failure"]
                failed_operation = split_label(failure.get("operation") or "") or operation
                if failed_operation is None or case_id not in cases:
                    self.unknown += 1
                    continue
                self.failures.append(Failure(
                    failed_operation, check["name"], failure.get("title") or check["name"],
                    failure.get("message") or "", data.get("phase") or "fuzzing", case_id,
                    _case(cases[case_id]["value"]), _interaction(interactions.get(case_id)),
                ))


def parse_lines(lines: list[str], error_chars: int) -> RoleReport:
    """error_chars 为运行错误消息保留的字符数(runtime.sources.reportErrorChars)。"""
    collector = _Collector(error_chars)
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            (name, data), = event.items()
        except (ValueError, AttributeError):
            collector.unknown += 1
            continue
        if name not in KNOWN_EVENTS or not isinstance(data, Mapping):
            collector.unknown += 1
            continue
        try:
            collector.event(name, data)
        except (KeyError, TypeError, AttributeError):
            collector.unknown += 1
    return RoleReport(collector.complete, tuple(collector.tested), tuple(collector.failures), collector.unknown,
                      collector.seed, collector.finished_at, tuple(collector.errors))


def parse(path: Path, error_chars: int) -> RoleReport:
    """报告文件不存在时视为不完整。"""
    if not path.is_file():
        return RoleReport(False)
    return parse_lines(path.read_text(encoding="utf-8", errors="replace").splitlines(), error_chars)
