"""会话记录的统一事件(architecture/02 2.6)：`data/runs/<运行编号>/transcripts/<角色>-<对象编号>.jsonl`，一行一个事件。

- 适配器把工具的输出逐行转换为 EventDraft，这里补上序号、时间、定位、工具、模型与会话后写入；
  工具不提供时间时取核心读到该行的时间。
- 同一角色与对象在一次运行中的每一次调用(含格式重试与调用方的再次运行)追加在同一文件中，attempt 为该文件内的调用序号。
- 文本、工具输入与输出写入前经 Redactor 脱敏；工具输出超过 16384 个字符的截断，完整内容在原始输出中。
- 不认识的事件记为 message、actor 为 system，原文放在 text 中，不丢弃。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.config import layers
from tightrein.domain.clock import Clock, format_iso
from tightrein.observability.redact import Redactor
from tightrein.runner.result import Usage

SESSION_START = "session-start"
MESSAGE = "message"
TOOL_CALL = "tool-call"
TOOL_RESULT = "tool-result"
USAGE = "usage"
ERROR = "error"
RESULT = "result"
USER = "user"
ASSISTANT = "assistant"
SYSTEM = "system"
TRUNCATED = "\n...(已截断，完整内容见原始输出)"


@dataclass(frozen=True)
class EventDraft:
    type: str
    actor: str
    text: str | None = None
    tool_name: str | None = None
    tool_input: Any = None
    tool_call_id: str | None = None
    tool_output: str | None = None
    is_error: bool | None = None
    usage: Usage | None = None
    timestamp: datetime | None = None
    session_id: str | None = None


def unknown(line: str) -> EventDraft:
    return EventDraft(MESSAGE, SYSTEM, text=line)


def truncate(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    if limit <= len(TRUNCATED):  # 上限比截断标记还短时只截断，不加标记，结果不超过上限
        return text[:limit]
    return text[:limit - len(TRUNCATED)] + TRUNCATED


def read(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TranscriptWriter:
    def __init__(self, path: Path, redactor: Redactor, clock: Clock, *, run_id: str, role: str,
                 subject_id: str, tool_output_chars: int | None = None) -> None:
        """tool_output_chars 为单次工具输出的最大字符数，缺省取 runtime.runner.toolOutputMaxChars 的核心缺省值。"""
        self.path = path
        self.tool_output_chars = int(layers.core_value("runtime.runner.toolOutputMaxChars")) \
            if tool_output_chars is None else tool_output_chars
        self.redactor = redactor
        self.clock = clock
        self.run_id = run_id
        self.role = role
        self.subject_id = subject_id
        existing = read(path)
        self.seq = max((event["seq"] for event in existing), default=0)
        self.attempt = max((event["attempt"] for event in existing), default=0)
        self.current: list[dict[str, Any]] = []

    def begin_call(self) -> int:
        """开始一次新的调用，返回它在文件中的序号。"""
        self.attempt += 1
        self.current = []
        return self.attempt

    def write(self, draft: EventDraft, *, tool: str, model: str | None, session_id: str | None) -> dict[str, Any]:
        if self.attempt == 0:
            self.begin_call()
        self.seq += 1
        redact = self.redactor.text
        event = {
            "seq": self.seq, "timestamp": format_iso(draft.timestamp or self.clock.now()), "runId": self.run_id,
            "role": self.role, "subjectId": self.subject_id, "attempt": self.attempt, "tool": tool, "model": model,
            "sessionId": draft.session_id or session_id, "type": draft.type, "actor": draft.actor,
            "text": None if draft.text is None else redact(draft.text), "toolName": draft.tool_name,
            "toolInput": self.redactor.value(draft.tool_input), "toolCallId": draft.tool_call_id,
            "toolOutput": truncate(None if draft.tool_output is None else redact(draft.tool_output),
                                   self.tool_output_chars),
            "isError": draft.is_error, "usage": None if draft.usage is None else draft.usage.to_dict(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.current.append(event)
        return event

    def write_all(self, drafts: Iterable[EventDraft], *, tool: str, model: str | None,
                  session_id: str | None) -> list[dict[str, Any]]:
        return [self.write(draft, tool=tool, model=model, session_id=session_id) for draft in drafts]

    def tool_calls(self) -> list[dict[str, Any]]:
        """本次调用中的工具调用事件，交给 guards 检查隐藏路径的读取。"""
        return [event for event in self.current if event["type"] == TOOL_CALL]
