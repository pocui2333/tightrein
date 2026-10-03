"""执行器结果(architecture/02 2.3、2.11)，与 runner/runner-result.schema.json 互转。

transcriptPath 与 guardReport 为相对运行目录的路径。所有失败都以结果返回，不向上抛异常。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from tightrein.contracts import validate
from tightrein.domain.enums import RunnerStatus
from tightrein.guards.report import Violation

SCHEMA = "runner/runner-result.schema.json"

TOOL_UNAVAILABLE = "tool-unavailable"
TOOL_ERROR = "tool-error"
TIMEOUT = "timeout"
TURN_LIMIT = "turn-limit"
COST_LIMIT = "cost-limit"
DAILY_BUDGET = "daily-budget"
REPLAY_MISSING = "replay-missing"
REPLAY_TASK_CHANGED = "replay-task-changed"
RESUME_UNSUPPORTED = "resume-unsupported"


class RunnerConfigError(Exception):
    """编程或配置错误：任务不合 schema、配置缺项、工具名未知等。只有这类错误向上抛出。"""


def _add(left: int | float | None, right: int | float | None) -> Any:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_usd: float | None = None
    cost_estimated: bool = False

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            _add(self.input_tokens, other.input_tokens), _add(self.output_tokens, other.output_tokens),
            _add(self.cached_input_tokens, other.cached_input_tokens), _add(self.cost_usd, other.cost_usd),
            self.cost_estimated or other.cost_estimated,
        )

    @staticmethod
    def total(usages: Iterable[Usage]) -> Usage:
        result = Usage()
        for usage in usages:
            result = result + usage
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputTokens": self.input_tokens, "outputTokens": self.output_tokens,
            "cachedInputTokens": self.cached_input_tokens, "costUsd": self.cost_usd,
            "costEstimated": self.cost_estimated,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Usage:
        return cls(data["inputTokens"], data["outputTokens"], data["cachedInputTokens"], data["costUsd"],
                   data["costEstimated"])


@dataclass(frozen=True)
class RunnerResult:
    status: RunnerStatus
    tool: str
    model: str | None = None
    error_type: str | None = None
    output: Mapping[str, Any] | None = None
    usage: Usage = Usage()
    duration_ms: int = 0
    attempts: int = 0
    session_id: str | None = None
    transcript_path: str | None = None
    guard_report: str | None = None
    violations: tuple[Violation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """按 schema 校验后返回；不合格时抛出 SchemaValidationError。"""
        data = {
            "status": self.status.value, "errorType": self.error_type,
            "output": None if self.output is None else dict(self.output), "usage": self.usage.to_dict(),
            "durationMs": self.duration_ms, "attempts": self.attempts, "tool": self.tool, "model": self.model,
            "sessionId": self.session_id, "transcriptPath": self.transcript_path, "guardReport": self.guard_report,
            "violations": [item.to_dict() for item in self.violations],
        }
        validate.check(SCHEMA, data)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunnerResult:
        validate.check(SCHEMA, data)
        return cls(
            status=RunnerStatus(data["status"]), tool=data["tool"], model=data["model"], error_type=data["errorType"],
            output=data["output"], usage=Usage.from_dict(data["usage"]), duration_ms=data["durationMs"],
            attempts=data["attempts"], session_id=data["sessionId"], transcript_path=data["transcriptPath"],
            guard_report=data["guardReport"],
            violations=tuple(Violation.from_dict(item) for item in data["violations"]),
        )
