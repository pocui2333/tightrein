"""Codex CLI 适配器(architecture/02 2.5、2.6、2.8)。

- 无人值守：`codex exec --json -C <workdir> --sandbox <只读或可写> --ask-for-approval never`，提示文本作为参数；
  --output-schema 传 schema 文件，最终消息即符合 schema 的 JSON；-o 另存最终消息；推理强度为
  `-c model_reasoning_effort=<强度>`。
- --json 输出 JSONL：thread.started(thread_id)、turn.started、item.started、item.completed、turn.completed(用量)、
  turn.failed、error。命令执行、文件修改、MCP 工具调用与联网搜索的 item.started 为工具调用，对应的 item.completed 为结果；
  助手消息与推理条目为 message；turn.started 与 item.updated 不含内容，不记录。
- 没有原生的轮数与费用上限，由核心按工具调用计数、按价格表估算费用；没有逐条放行命令的参数，命令范围由沙箱与第二层
  检查兜底；exec 没有联网检索的开关，联网任务不交给 Codex CLI。
- 格式重试：`codex exec ... resume <会话 ID> <重试说明>` 续接同一会话。
- 交互：`codex -C <workdir> --sandbox workspace-write --ask-for-approval on-request "<首条提示>"`(会话中调用的
  tightrein fix 子命令要写修复 worktree)，首条提示为提示文件内容加首条输入；续接为 `codex resume <会话 ID>`；
  结束后在 `~/.codex/sessions/` 中按工作目录与开始时间找到会话记录(rollout 文件，第一行为 session_meta)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tightrein.domain.clock import parse_iso
from tightrein.runner.adapters.base import (
    ENDED_COMPLETED,
    ENDED_ERROR,
    FINALIZE_PROMPT,
    InvocationFiles,
    ParsedRun,
    RetryContext,
    SessionRef,
    json_lines,
    load_line,
    session_clock_skew,
    text_of,
    timestamp,
)
from tightrein.runner.process import Invocation
from tightrein.runner.result import RunnerConfigError, Usage
from tightrein.runner.task import RunnerTask
from tightrein.runner.transcript import (
    ASSISTANT,
    ERROR,
    MESSAGE,
    RESULT,
    SESSION_START,
    SYSTEM,
    TOOL_CALL,
    TOOL_RESULT,
    USAGE,
    USER,
    EventDraft,
    unknown,
)

REASONING_EFFORT = "model_reasoning_effort"
TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})
MESSAGE_ITEMS = frozenset({"agent_message", "reasoning"})
SILENT = frozenset({"turn.started", "item.updated"})
FAILED = "failed"
ROLES = {"user": USER, "assistant": ASSISTANT}


def usage_of(data: Mapping[str, Any] | None) -> Usage:
    if not data:
        return Usage()
    return Usage(data.get("input_tokens"), data.get("output_tokens"), data.get("cached_input_tokens"))


def _tool_call(item: Mapping[str, Any], when: datetime | None) -> EventDraft:
    kind = item["type"]
    if kind == "command_execution":
        name, arguments = "shell", {"command": item.get("command")}
    elif kind == "file_change":
        name, arguments = "file_change", {"changes": item.get("changes")}
    elif kind == "mcp_tool_call":
        name, arguments = f"mcp:{item.get('server')}/{item.get('tool')}", item.get("arguments")
    else:
        name, arguments = "web_search", {"query": item.get("query")}
    return EventDraft(TOOL_CALL, ASSISTANT, tool_name=name, tool_input=arguments, tool_call_id=item.get("id"),
                      timestamp=when)


def _tool_result(item: Mapping[str, Any], when: datetime | None) -> EventDraft:
    kind = item["type"]
    failed = item.get("status") == FAILED
    if kind == "command_execution":
        output = item.get("aggregated_output")
        failed = failed or item.get("exit_code") not in (0, None)
    elif kind == "mcp_tool_call":
        error = item.get("error")
        output = json.dumps(error if error else item.get("result"), ensure_ascii=False)
        failed = failed or bool(error)
    else:
        output = json.dumps({key: value for key, value in item.items() if key not in ("id", "type")},
                            ensure_ascii=False)
    return EventDraft(TOOL_RESULT, USER, tool_call_id=item.get("id"), tool_output=output, is_error=failed,
                      timestamp=when)


class CodexAdapter:
    name = "codex"
    supports_schema = True
    supports_turn_limit = False
    supports_budget_limit = False
    supports_resume_by_id = True
    supports_image_input = True
    env_names = ("CODEX_HOME",)

    def __init__(self, home: Path, clock_skew: timedelta | None = None) -> None:
        self.home = home
        self.clock_skew = clock_skew if clock_skew is not None else session_clock_skew()

    def _options(self, task: RunnerTask, sandbox: str, approval: str, model: str | None,
                 effort: str | None) -> list[str]:
        argv = ["-C", str(task.workdir), "--sandbox", sandbox, "--ask-for-approval", approval]
        if model is not None:
            argv += ["-m", model]
        if effort is not None:
            argv += ["-c", f"{REASONING_EFFORT}={effort}"]
        return argv

    def build(self, task: RunnerTask, files: InvocationFiles, *, executable: str, model: str | None,
              env: Mapping[str, str], retry: RetryContext | None, effort: str | None = None) -> Invocation:
        if task.web:
            raise RunnerConfigError("codex exec 没有联网检索的开关，联网任务须使用其他工具")
        sandbox = "read-only" if task.readonly else "workspace-write"
        argv = [executable, "exec", "--json", *self._options(task, sandbox, "never", model, effort)]
        if task.output_schema is not None:
            argv += ["--output-schema", str(files.schema)]
        argv += ["-o", str(files.last_message)]
        if retry is not None and retry.session_id is not None:
            argv += ["resume", retry.session_id, retry.note]
        else:
            argv.append(files.prompt.read_text(encoding="utf-8"))
        return Invocation(tuple(argv), task.workdir, env)

    def new_session_id(self) -> str | None:
        return None

    def build_interactive(self, task: RunnerTask, files: InvocationFiles, first_input: str, *, executable: str,
                          model: str | None, env: Mapping[str, str], session: SessionRef | None,
                          resume: bool, effort: str | None = None) -> Invocation:
        options = self._options(task, "workspace-write", "on-request", model, effort)
        if resume:
            if session is None:
                raise ValueError("续接需要会话 ID")
            argv = [executable, "resume", session.session_id, *options, first_input]
        else:
            prompt = files.prompt.read_text(encoding="utf-8")
            argv = [executable, *options, f"{prompt}\n# 首条输入\n{first_input}"]
        return Invocation(tuple(argv), task.workdir, env)

    def build_finalize(self, task: RunnerTask, files: InvocationFiles, session: SessionRef, *, executable: str,
                       model: str | None, env: Mapping[str, str], effort: str | None = None) -> Invocation:
        argv = [executable, "exec", "--json", *self._options(task, "read-only", "never", model, effort),
                "--output-schema", str(files.schema), "resume", session.session_id, FINALIZE_PROMPT]
        return Invocation(tuple(argv), task.workdir, env)

    def convert(self, line: str) -> list[EventDraft]:
        data = load_line(line)
        if data is None:
            return [unknown(line)]
        kind = data.get("type")
        when = timestamp(data)
        item = data.get("item") or {}
        if kind in SILENT:
            return []
        if kind == "thread.started":
            return [EventDraft(SESSION_START, SYSTEM, timestamp=when, session_id=data.get("thread_id"))]
        if kind == "item.started" and item.get("type") in TOOL_ITEMS:
            return [_tool_call(item, when)]
        if kind == "item.started" and item.get("type") in MESSAGE_ITEMS:
            return []
        if kind == "item.completed" and item.get("type") in MESSAGE_ITEMS:
            return [EventDraft(MESSAGE, ASSISTANT, text=item.get("text"), timestamp=when)]
        if kind == "item.completed" and item.get("type") in TOOL_ITEMS:
            return [_tool_result(item, when)]
        if kind == "item.completed" and item.get("type") == "error":
            return [EventDraft(ERROR, SYSTEM, text=item.get("message"), timestamp=when)]
        if kind == "turn.completed":
            return [EventDraft(USAGE, SYSTEM, usage=usage_of(data.get("usage")), timestamp=when)]
        if kind == "turn.failed":
            return [EventDraft(ERROR, SYSTEM, text=(data.get("error") or {}).get("message"), timestamp=when)]
        if kind == "error":
            return [EventDraft(ERROR, SYSTEM, text=data.get("message"), timestamp=when)]
        return [unknown(line)]

    def closing_events(self, parsed: ParsedRun) -> list[EventDraft]:
        """Codex CLI 没有 result 事件，以最后一条助手消息作为 result。"""
        return [] if parsed.final_text is None else [EventDraft(RESULT, ASSISTANT, text=parsed.final_text)]

    def parse(self, raw_stdout: Path, exit_code: int) -> ParsedRun:
        session_id = None
        final_text = None
        usage = Usage()
        turns = 0
        errors = []
        for data in json_lines(raw_stdout):
            kind = data.get("type")
            item = data.get("item") or {}
            if kind == "thread.started":
                session_id = data.get("thread_id")
            elif kind == "item.started" and item.get("type") in TOOL_ITEMS:
                turns += 1
            elif kind == "item.completed" and item.get("type") == "agent_message":
                final_text = item.get("text")
            elif kind == "turn.completed":
                usage = usage + usage_of(data.get("usage"))
            elif kind == "turn.failed":
                errors.append((data.get("error") or {}).get("message") or "turn.failed")
            elif kind == "error":
                errors.append(data.get("message") or "error")
        if errors or exit_code != 0:
            message = "；".join(errors) if errors else f"退出码 {exit_code}"
            return ParsedRun(session_id, final_text, None, usage, turns, ENDED_ERROR, message)
        return ParsedRun(session_id, final_text, None, usage, turns, ENDED_COMPLETED)

    # 交互会话

    def locate_session(self, workdir: Path, started_at: datetime, session_id: str | None) -> SessionRef | None:
        """在 sessions 目录中找工作目录相同、开始时间不早于 started_at 的最早一个会话。"""
        found: list[tuple[datetime, str, Path]] = []
        for path in sorted((self.home / "sessions").rglob("rollout-*.jsonl")):
            with open(path, encoding="utf-8") as handle:
                first = load_line(handle.readline()) or {}
            meta = first.get("payload") or {}
            if first.get("type") != "session_meta" or meta.get("cwd") != str(workdir):
                continue
            started = parse_iso(meta.get("timestamp") or first.get("timestamp"))
            if started >= started_at - self.clock_skew and (session_id is None or meta.get("id") == session_id):
                found.append((started, meta.get("id"), path))
        if not found:
            return None
        _, found_id, path = min(found)
        return SessionRef(found_id, path)

    def _response_item(self, payload: Mapping[str, Any], when: datetime | None, line: str) -> list[EventDraft]:
        kind = payload.get("type")
        if kind == "message":
            actor = ROLES.get(payload.get("role", ""), SYSTEM)
            return [EventDraft(MESSAGE, actor, text=text_of(payload.get("content")), timestamp=when)]
        if kind == "reasoning":
            summary = "".join(part.get("text", "") for part in payload.get("summary") or [])
            return [EventDraft(MESSAGE, ASSISTANT, text=summary, timestamp=when)]
        if kind == "function_call":
            arguments = payload.get("arguments")
            parsed = load_line(arguments) if isinstance(arguments, str) else arguments
            return [EventDraft(TOOL_CALL, ASSISTANT, tool_name=payload.get("name"),
                               tool_input=parsed if parsed is not None else arguments,
                               tool_call_id=payload.get("call_id"), timestamp=when)]
        if kind == "function_call_output":
            output = payload.get("output")
            text = output.get("output") if isinstance(output, dict) else output
            return [EventDraft(TOOL_RESULT, USER, tool_call_id=payload.get("call_id"),
                               tool_output=text if isinstance(text, str) else json.dumps(text, ensure_ascii=False),
                               timestamp=when)]
        return [unknown(line)]

    def session_events(self, session: SessionRef) -> list[EventDraft]:
        if session.path is None:
            return []
        drafts: list[EventDraft] = []
        for line in session.path.read_text(encoding="utf-8").splitlines():
            data = load_line(line)
            if data is None:
                drafts.append(unknown(line))
                continue
            when = timestamp(data)
            payload = data.get("payload") or {}
            kind = data.get("type")
            if kind == "session_meta":
                drafts.append(EventDraft(SESSION_START, SYSTEM, timestamp=when, session_id=payload.get("id")))
            elif kind == "response_item":
                drafts += self._response_item(payload, when, line)
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                drafts.append(EventDraft(USAGE, SYSTEM, usage=usage_of(info.get("last_token_usage")), timestamp=when))
            elif kind not in ("event_msg", "turn_context"):
                drafts.append(unknown(line))
        return drafts
