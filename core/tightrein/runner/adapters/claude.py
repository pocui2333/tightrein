"""Claude Code 适配器(architecture/02 2.5、2.6、2.8)。

- 无人值守：`claude -p --output-format stream-json --verbose`，提示文件经标准输入传入；逐行事件即会话记录，最后一行
  result 事件带最终文本、session_id、usage、total_cost_usd，按 schema 约束时结构化结果在 structured_output。
- 访问级别：--tools 只列出读取、搜索与 Bash(可写时加 Edit、Write)；--allowedTools 列出 Read、Grep、Glob 与每条允许命令
  `Bash(<命令前缀> *)`；只读为 --permission-mode dontAsk，可写为 acceptEdits。两个列表都以逗号分隔写成一个参数。
- 输出 schema 经 --json-schema 以文本传入；Claude Code 的校验器不认 2020-12 的 `$schema` 声明，传入前去掉该键，
  各 schema 未使用该版本独有的关键字。
- 上限：--max-turns、--max-budget-usd 由工具原生保证；推理强度为 --effort。格式重试以 --resume 续接同一会话，重试说明经标准输入传入。
- 交互：核心预先生成会话 ID(--session-id)，任务说明以 --append-system-prompt-file 传入，首条输入放在 `--` 之后；
  结束后读取 `~/.claude/projects/<工作目录>/<会话 ID>.jsonl`，其中每条助手消息带本次 API 调用的用量。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.runner.adapters.base import (
    ENDED_BUDGET_LIMIT,
    ENDED_COMPLETED,
    ENDED_ERROR,
    ENDED_TURN_LIMIT,
    FINALIZE_PROMPT,
    InvocationFiles,
    ParsedRun,
    RetryContext,
    SessionRef,
    json_lines,
    load_line,
    read_dirs,
    text_of,
    timestamp,
)
from tightrein.runner.process import Invocation
from tightrein.runner.result import Usage
from tightrein.runner.task import RunnerTask

SCHEMA_DIALECT_KEY = "$schema"
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

READ_TOOLS = ("Read", "Grep", "Glob")
WRITE_TOOLS = ("Edit", "Write")
WEB_TOOLS = ("WebSearch", "WebFetch")
SHELL = "Bash"
SUCCESS = "success"
ENDINGS = {"error_max_turns": ENDED_TURN_LIMIT, "error_max_budget_usd": ENDED_BUDGET_LIMIT}


def usage_of(data: Mapping[str, Any] | None, cost: float | None = None) -> Usage:
    """输入 token 为未缓存、写入缓存与读取缓存三者之和，其中读取缓存的计为 cachedInputTokens。"""
    if not data:
        return Usage(cost_usd=cost)
    cached = data.get("cache_read_input_tokens")
    parts = [data.get("input_tokens"), data.get("cache_creation_input_tokens"), cached]
    known = [part for part in parts if part is not None]
    return Usage(sum(known) if known else None, data.get("output_tokens"), cached, cost, False)


def cli_schema(path: Path) -> str:
    """传给 --json-schema 的 schema 文本：去掉 `$schema` 声明，其余原样。"""
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema.pop(SCHEMA_DIALECT_KEY, None)
    return json.dumps(schema, ensure_ascii=False)


class ClaudeAdapter:
    name = "claude"
    supports_schema = True
    supports_turn_limit = True
    supports_budget_limit = True
    supports_resume_by_id = True
    supports_image_input = True
    env_names = ("CLAUDE_CONFIG_DIR",)

    def __init__(self, home: Path, new_id: Callable[[], str] = lambda: str(uuid.uuid4())) -> None:
        self.home = home
        self._new_id = new_id

    # 参数

    def _permissions(self, task: RunnerTask) -> list[str]:
        tools = [*READ_TOOLS, SHELL, *(() if task.readonly else WRITE_TOOLS), *(WEB_TOOLS if task.web else ())]
        allowed = [*READ_TOOLS, *(f"{SHELL}({command} *)" for command in task.allowed_commands),
                   *(WEB_TOOLS if task.web else ())]
        mode = "dontAsk" if task.readonly else "acceptEdits"
        argv = ["--tools", ",".join(tools), "--allowedTools", ",".join(allowed), "--permission-mode", mode]
        for directory in read_dirs(task):
            argv += ["--add-dir", directory]
        return argv

    def build(self, task: RunnerTask, files: InvocationFiles, *, executable: str, model: str | None,
              env: Mapping[str, str], retry: RetryContext | None, effort: str | None = None) -> Invocation:
        argv = [executable, "-p", "--output-format", "stream-json", "--verbose", *self._permissions(task)]
        if task.limits.max_turns is not None:
            argv += ["--max-turns", str(task.limits.max_turns)]
        if task.limits.max_cost_usd is not None:
            argv += ["--max-budget-usd", str(task.limits.max_cost_usd)]
        if model is not None:
            argv += ["--model", model]
        if effort is not None:
            argv += ["--effort", effort]
        if task.output_schema is not None:
            argv += ["--json-schema", cli_schema(files.schema)]
        if retry is not None and retry.session_id is not None:
            argv += ["--resume", retry.session_id]
            stdin = retry.note.encode("utf-8")
        else:
            stdin = files.prompt.read_bytes()
        return Invocation(tuple(argv), task.workdir, env, stdin)

    def new_session_id(self) -> str | None:
        return self._new_id()

    def build_interactive(self, task: RunnerTask, files: InvocationFiles, first_input: str, *, executable: str,
                          model: str | None, env: Mapping[str, str], session: SessionRef | None,
                          resume: bool, effort: str | None = None) -> Invocation:
        if session is None:
            raise ValueError("Claude Code 的交互会话需要预先生成的会话 ID")
        argv = [executable, "--resume" if resume else "--session-id", session.session_id]
        if not resume:
            argv += ["--append-system-prompt-file", str(files.prompt)]
        argv += self._permissions(task)
        if model is not None:
            argv += ["--model", model]
        if effort is not None:
            argv += ["--effort", effort]
        return Invocation(tuple([*argv, "--", first_input]), task.workdir, env)

    def build_finalize(self, task: RunnerTask, files: InvocationFiles, session: SessionRef, *, executable: str,
                       model: str | None, env: Mapping[str, str], effort: str | None = None) -> Invocation:
        argv = [executable, "-p", "--resume", session.session_id, "--output-format", "json", "--json-schema",
                cli_schema(files.schema)]
        if model is not None:
            argv += ["--model", model]
        if effort is not None:
            argv += ["--effort", effort]
        return Invocation(tuple(argv), task.workdir, env, FINALIZE_PROMPT.encode("utf-8"))

    # 输出

    def _blocks(self, data: Mapping[str, Any], when: datetime | None) -> list[EventDraft]:
        role = data.get("type")
        content = (data.get("message") or {}).get("content")
        if isinstance(content, str):
            return [EventDraft(MESSAGE, USER if role == "user" else ASSISTANT, text=content, timestamp=when)]
        drafts = []
        for block in content or []:
            kind = block.get("type")
            if kind == "text":
                drafts.append(EventDraft(MESSAGE, USER if role == "user" else ASSISTANT, text=block.get("text"),
                                         timestamp=when))
            elif kind == "tool_use":
                drafts.append(EventDraft(TOOL_CALL, ASSISTANT, tool_name=block.get("name"),
                                         tool_input=block.get("input"), tool_call_id=block.get("id"), timestamp=when))
            elif kind == "tool_result":
                drafts.append(EventDraft(TOOL_RESULT, USER, tool_call_id=block.get("tool_use_id"),
                                         tool_output=text_of(block.get("content")),
                                         is_error=bool(block.get("is_error")), timestamp=when))
            else:
                drafts.append(unknown(json.dumps(block, ensure_ascii=False)))
        return drafts

    def convert(self, line: str) -> list[EventDraft]:
        data = load_line(line)
        if data is None:
            return [unknown(line)]
        when = timestamp(data)
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            return [EventDraft(SESSION_START, SYSTEM, text=f"model {data.get('model')}", timestamp=when,
                               session_id=data.get("session_id"))]
        if kind == "system" and data.get("subtype") == "api_retry":
            text = f"API 重试 第 {data.get('attempt')} 次：{data.get('error')}(状态 {data.get('error_status')})"
            return [EventDraft(ERROR, SYSTEM, text=text, timestamp=when)]
        if kind in ("assistant", "user"):
            return self._blocks(data, when)
        if kind == "result":
            drafts = [EventDraft(USAGE, SYSTEM, usage=usage_of(data.get("usage"), data.get("total_cost_usd")),
                                 timestamp=when),
                      EventDraft(RESULT, ASSISTANT, text=data.get("result"), timestamp=when)]
            if data.get("is_error") or data.get("subtype") != SUCCESS:
                drafts.append(EventDraft(ERROR, SYSTEM, text=f"{data.get('subtype')}：{data.get('result')}",
                                         timestamp=when))
            return drafts
        return [unknown(line)]

    def closing_events(self, parsed: ParsedRun) -> list[EventDraft]:
        return []

    def parse(self, raw_stdout: Path, exit_code: int) -> ParsedRun:
        records = json_lines(raw_stdout)
        session_id = next((item.get("session_id") for item in records if item.get("type") == "system"), None)
        results = [item for item in records if item.get("type") == "result"]
        if not results:
            return ParsedRun(session_id, None, None, Usage(), None, ENDED_ERROR, "输出中没有 result 事件")
        result = results[-1]
        subtype = result.get("subtype")
        if subtype == SUCCESS and not result.get("is_error") and exit_code == 0:
            ended, message = ENDED_COMPLETED, None
        else:
            ended, message = ENDINGS.get(subtype or "", ENDED_ERROR), f"{subtype}：{result.get('result')}"
        return ParsedRun(
            result.get("session_id") or session_id, result.get("result"), result.get("structured_output"),
            usage_of(result.get("usage"), result.get("total_cost_usd")), result.get("num_turns"), ended, message,
        )

    # 交互会话

    def session_file(self, workdir: Path, session_id: str) -> Path:
        return self.home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workdir)) / f"{session_id}.jsonl"

    def locate_session(self, workdir: Path, started_at: datetime, session_id: str | None) -> SessionRef | None:
        if session_id is None:
            return None
        path = self.session_file(workdir, session_id)
        return SessionRef(session_id, path if path.is_file() else None)

    def session_events(self, session: SessionRef) -> list[EventDraft]:
        if session.path is None:
            return []
        drafts: list[EventDraft] = []
        counted: set[str] = set()
        for data in json_lines(session.path):
            if data.get("type") not in ("assistant", "user"):
                drafts.append(unknown(json.dumps(data, ensure_ascii=False)))
                continue
            drafts += self._blocks(data, timestamp(data))
            message = data.get("message") or {}
            message_id = message.get("id")
            if data.get("type") == "assistant" and message.get("usage") and message_id not in counted:
                counted.add(message_id)
                drafts.append(EventDraft(USAGE, SYSTEM, usage=usage_of(message["usage"]), timestamp=timestamp(data)))
        return drafts
