"""回放适配器(architecture/02 2.12)：不启动任何进程，按 (role, subjectId, attempt) 取录制，使上层模块在不调用模型的
情况下走与真实运行相同的代码路径：schema 校验、guards 与预算累计照常进行。

- 第 n 次调用(格式重试为第 2 次)写入录制会话记录中第 n 个调用的事件；结构化结果取 result.json 的 output，第一次调用
  有 first-call.json 时取它；用量只计在第一次调用上，累计值与录制时相同。
- 有 changes.patch 的，第一次调用时用 `git apply` 应用到 workdir，使 guards.after 与 diff 规则照常生效。
- 录制的状态为 limit-reached 或 failed 时原样返回；为 schema-invalid 时 output 为空，校验自然不通过；
  guard-violation 由 changes.patch 在回放中重新触发，不直接复制录制的违规项。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tightrein.domain.enums import RunnerStatus
from tightrein.runner.adapters.base import ENDED_COMPLETED, ENDED_ERROR, ParsedRun
from tightrein.runner.recording import Recording, RecordingSet, task_sha256
from tightrein.runner.result import REPLAY_MISSING, REPLAY_TASK_CHANGED, TOOL_ERROR, Usage
from tightrein.runner.task import RunnerTask
from tightrein.runner.transcript import EventDraft
from tightrein.vcs.errors import VcsError
from tightrein.vcs.process import VcsProcess

NAME = "replay"
FORCED = frozenset({RunnerStatus.LIMIT_REACHED, RunnerStatus.FAILED})


@dataclass(frozen=True)
class ReplayCall:
    parsed: ParsedRun
    events: list[EventDraft]
    tool: str
    model: str | None
    forced: tuple[RunnerStatus, str | None] | None = None


def draft_of(event: Mapping[str, Any]) -> EventDraft:
    usage = event.get("usage")
    return EventDraft(
        event["type"], event["actor"], text=event.get("text"), tool_name=event.get("toolName"),
        tool_input=event.get("toolInput"), tool_call_id=event.get("toolCallId"), tool_output=event.get("toolOutput"),
        is_error=event.get("isError"), usage=None if usage is None else Usage.from_dict(usage),
        session_id=event.get("sessionId"),
    )


def _calls(recording: Recording) -> list[list[dict[str, Any]]]:
    groups: dict[int, list[dict[str, Any]]] = {}
    for event in recording.events:
        groups.setdefault(event["attempt"], []).append(event)
    return [groups[attempt] for attempt in sorted(groups)]


class ReplayAdapter:
    name = NAME

    def __init__(self, recordings: RecordingSet, process: VcsProcess) -> None:
        self.recordings = recordings
        self.process = process

    def missing(self, task: RunnerTask, error_type: str, message: str) -> ReplayCall:
        """录制不能回放：没有录制、任务已变或改动无法应用。"""
        parsed = ParsedRun(None, None, None, Usage(), None, ENDED_ERROR, message)
        return ReplayCall(parsed, [], task.tool or NAME, task.model, (RunnerStatus.FAILED, error_type))

    def call(self, task: RunnerTask, number: int) -> ReplayCall:
        recording = self.recordings.find(task.role, task.subject_id, task.attempt)
        if recording is None:
            return self.missing(task, REPLAY_MISSING,
                                f"录制集中没有 {task.role}-{task.subject_id} 第 {task.attempt} 次的录制")
        if recording.task_sha256 != task_sha256(task):
            return self.missing(task, REPLAY_TASK_CHANGED, "任务的提示、schema 或访问级别与录制时不同，需要重新录制")
        result = recording.result
        if number == 1 and recording.patch is not None:
            try:
                self.process.git(task.workdir, "apply", "--whitespace=nowarn", str(recording.patch))
            except VcsError as error:
                return self.missing(task, TOOL_ERROR, f"录制的改动无法应用到 {task.workdir}：{error}")
        calls = _calls(recording)
        events = [draft_of(event) for event in calls[number - 1]] if number <= len(calls) else []
        structured = recording.first_call if number == 1 and recording.has_first_call else result["output"]
        usage = Usage.from_dict(result["usage"]) if number == 1 else Usage()
        status = RunnerStatus(result["status"])
        forced = (status, result["errorType"]) if status in FORCED else None
        parsed = ParsedRun(result["sessionId"], structured if isinstance(structured, str) else None,
                           structured if isinstance(structured, dict) else None, usage, None, ENDED_COMPLETED)
        return ReplayCall(parsed, events, result["tool"], result["model"], forced)
