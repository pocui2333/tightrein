"""适配器的协议与共用的数据结构(architecture/02 2.4)。

适配器只负责调用方式与格式转换，不做业务判断：build 生成参数列表，convert 把工具输出的一行转换为统一事件，
parse 从整个原始输出中取会话、最终文本、结构化结果、用量与结束原因。交互会话结束后，locate_session 与 session_events
读取工具本机保存的会话记录。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from tightrein.config import layers
from tightrein.domain.clock import parse_iso
from tightrein.runner.process import Invocation
from tightrein.runner.result import Usage
from tightrein.runner.task import RunnerTask
from tightrein.runner.transcript import EventDraft

ENDED_COMPLETED = "completed"
ENDED_TURN_LIMIT = "turn-limit"
ENDED_BUDGET_LIMIT = "budget-limit"
ENDED_ERROR = "error"
FINALIZE_PROMPT = "会话已结束。请按给定的 schema 输出本次会话的结构化结果，只输出一个 JSON 对象。"


def session_clock_skew(seconds: float | None = None) -> timedelta:
    """按开始时间查找工具本机会话记录时容许的时钟偏差；缺省取 runtime.runner.sessionClockSkewSeconds 的核心缺省值。"""
    value = layers.core_value("runtime.runner.sessionClockSkewSeconds") if seconds is None else seconds
    return timedelta(seconds=float(value))


@dataclass(frozen=True)
class InvocationFiles:
    """本次调用写在 `raw/runner/<角色>-<对象编号>/` 下的文件。"""

    directory: Path
    call: int = 1

    @property
    def prompt(self) -> Path:
        return self.directory / "prompt.md"

    @property
    def schema(self) -> Path:
        return self.directory / "schema.json"

    @property
    def last_message(self) -> Path:
        return self.directory / "last-message.txt"

    @property
    def stdout(self) -> Path:
        return self.directory / ("stdout.jsonl" if self.call == 1 else f"stdout.{self.call}.jsonl")

    @property
    def result(self) -> Path:
        return self.directory / "result.json"

    @property
    def started(self) -> Path:
        """调用开始时写下的工具、模型与开始时间；比 result.json 新时表示调用正在进行(tightrein watch 读取)。"""
        return self.directory / "started.json"


@dataclass(frozen=True)
class RetryContext:
    """格式重试：note 为重试说明；session_id 不为空且工具支持按会话续接时续接同一会话。"""

    note: str
    session_id: str | None = None


@dataclass(frozen=True)
class ParsedRun:
    """ended_by 为 completed、turn-limit、budget-limit(工具以原生上限结束)或 error。"""

    session_id: str | None
    final_text: str | None
    structured: dict[str, Any] | None
    usage: Usage
    turns: int | None
    ended_by: str
    error_message: str | None = None


@dataclass(frozen=True)
class SessionRef:
    """工具本机保存的会话：path 为会话记录文件。"""

    session_id: str
    path: Path | None = None


class Adapter(Protocol):
    name: str
    supports_schema: bool
    supports_turn_limit: bool
    supports_budget_limit: bool
    supports_resume_by_id: bool
    supports_image_input: bool
    env_names: tuple[str, ...]

    def build(self, task: RunnerTask, files: InvocationFiles, *, executable: str, model: str | None,
              env: Mapping[str, str], retry: RetryContext | None, effort: str | None = None) -> Invocation: ...

    def build_interactive(self, task: RunnerTask, files: InvocationFiles, first_input: str, *, executable: str,
                          model: str | None, env: Mapping[str, str], session: SessionRef | None,
                          resume: bool, effort: str | None = None) -> Invocation: ...

    def build_finalize(self, task: RunnerTask, files: InvocationFiles, session: SessionRef, *, executable: str,
                       model: str | None, env: Mapping[str, str], effort: str | None = None) -> Invocation: ...

    def convert(self, line: str) -> list[EventDraft]: ...

    def closing_events(self, parsed: ParsedRun) -> list[EventDraft]: ...

    def parse(self, raw_stdout: Path, exit_code: int) -> ParsedRun: ...

    def new_session_id(self) -> str | None: ...

    def locate_session(self, workdir: Path, started_at: datetime, session_id: str | None) -> SessionRef | None: ...

    def session_events(self, session: SessionRef) -> list[EventDraft]: ...


def to_events(adapter: Adapter, raw_stdout: Path) -> Iterator[EventDraft]:
    """原始输出的统一事件；整个输出是一个跨多行的 JSON 对象(--output-format json)时作为一行转换。"""
    records = json_lines(raw_stdout)
    lines = [line for line in raw_stdout.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(records) == 1 and len(lines) > 1 and load_line(lines[0]) is None:
        yield from adapter.convert(json.dumps(records[0], ensure_ascii=False))
        return
    for line in lines:
        yield from adapter.convert(line)


def load_line(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def json_lines(path: Path) -> list[dict[str, Any]]:
    """逐行解析的 JSON 对象；整个文件是一个(可能跨多行的)JSON 对象时返回它。"""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    parsed = [load_line(line) for line in lines]
    if lines and any(item is None for item in parsed):
        whole = load_line(text)
        return [whole] if whole is not None else [item for item in parsed if item is not None]
    return [item for item in parsed if item is not None]


def timestamp(data: Mapping[str, Any]) -> datetime | None:
    value = data.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


def text_of(content: Any) -> str:
    """消息内容可能是字符串，也可能是 `{type: text, text}` 等片段的列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def read_dirs(task: RunnerTask) -> list[str]:
    """readPaths 所在的目录，去重并保持顺序。"""
    seen: list[str] = []
    for path in task.read_paths:
        parent = str(Path(path).parent)
        if parent not in seen:
            seen.append(parent)
    return seen
