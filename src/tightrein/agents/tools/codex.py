"""Codex CLI 适配器；参数对照、已验证版本与不支持的项见 codex.md。"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.agents.params import Access, CallParams, Model
from tightrein.agents.result import CallStatus
from tightrein.agents.tools import (
    NOTHING,
    CallConfigError,
    LineEvent,
    Parsed,
    Resume,
    joined,
    json_records,
    load_line,
    quota_limit,
    read_dirs,
    resets_after,
    resets_at_clock,
    window_for,
    write_schema,
)
from tightrein.protocol.handoff import Tokens
from tightrein.protocol.process import Command

TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})
ITEM_META = frozenset({"id", "type", "status"})  # 工具调用条目中不是参数的字段
SHELLS = frozenset({"bash", "sh", "zsh", "/bin/bash", "/bin/sh", "/bin/zsh"})
QUOTA = re.compile(r"hit your usage limit|usage limit (?:reached|exceeded)", re.IGNORECASE)
TRY_AGAIN_IN = re.compile(r"try again in\s+([^.\n]+)", re.IGNORECASE)
TRY_AGAIN_AT = re.compile(r"try again at\s+([^.\n]+)", re.IGNORECASE)
AUTH = re.compile(r"not logged in|401 unauthorized|unauthorized|refresh token|please (?:re-?)?log ?in|codex login",
                  re.IGNORECASE)
REFUSAL = re.compile(r"content_filter|flagged as potentially violating|violates? (?:our|the) usage polic",
                     re.IGNORECASE)


class CodexAdapter:
    name = "codex"
    native_turns = False
    checks_commands = False  # 没有逐条放行命令的参数：命令范围靠沙箱与程序按白名单检查
    env_names: tuple[str, ...] = ("CODEX_HOME",)

    def env_values(self, params: CallParams) -> dict[str, str]:
        return {}

    def build(self, params: CallParams, model: Model, *, executable: str, env: dict[str, str],
              schema: dict[str, Any] | None, scratch: Path, resume: Resume | None) -> Command:
        if params.network:
            raise CallConfigError(f"{params.point}：codex exec 没有联网检索的开关，联网任务须路由到其他工具")
        finalize = resume is not None and resume.finalize
        sandbox = "read-only" if params.access is Access.READ or finalize else "workspace-write"
        # exec 的 --help(0.153.4)不列 --ask-for-approval，改用配置项覆盖，任何子命令都认
        argv = [executable, "exec", "--json", "-C", str(params.workdir), "--sandbox", sandbox,
                "-c", "approval_policy=never"]
        if model.model:
            argv += ["-m", model.model]
        if model.effort:
            argv += ["-c", f"model_reasoning_effort={model.effort}"]
        for directory in read_dirs(params):
            argv += ["--add-dir", directory]
        if schema is not None:
            argv += ["--output-schema", str(write_schema(schema, scratch))]
        argv += ["-o", str(scratch / "last-message.txt")]
        argv += ["resume", resume.session_id, resume.note] if resume is not None else [params.prompt]
        return Command(argv=tuple(argv), cwd=params.workdir, env=env)

    def parse_line(self, line: str) -> LineEvent:
        data = load_line(line)
        if data is None:
            return NOTHING
        kind = data.get("type")
        item = data.get("item") or {}
        if kind == "item.started" and item.get("type") in TOOL_ITEMS:
            command = item.get("command")
            commands = (unwrap(command),) if item.get("type") == "command_execution" and command else ()
            arguments = {key: value for key, value in item.items() if key not in ITEM_META}
            return LineEvent(tool_calls=1, commands=commands, tool_inputs=((str(item.get("type")), arguments),))
        if kind == "turn.completed":
            return LineEvent(tokens=tokens_of(data.get("usage")))
        return NOTHING

    def parse(self, stdout: str, stderr: str, exit_code: int | None, now: datetime) -> Parsed:
        """没有 result 事件：以最后一条 agent_message 作结果；命令退出码非 0 只是该命令失败，不算工具失败。"""
        parsed = Parsed(CallStatus.OK, turns=0)
        errors: list[str] = []
        for data in json_records(stdout):
            kind = data.get("type")
            item = data.get("item") or {}
            if kind == "thread.started":
                parsed.session_id = data.get("thread_id")
            elif kind == "item.started" and item.get("type") in TOOL_ITEMS:
                parsed.turns = (parsed.turns or 0) + 1
            elif kind == "item.completed" and item.get("type") == "agent_message":
                parsed.text = item.get("text")
            elif kind == "turn.completed":
                parsed.tokens.add(tokens_of(data.get("usage")))
            elif kind == "turn.failed":
                errors.append((data.get("error") or {}).get("message") or "turn.failed")
            elif kind == "error":
                errors.append(data.get("message") or "error")
        if not errors and exit_code == 0:
            return parsed
        parsed.status = CallStatus.FAILED
        parsed.error = "；".join(errors) if errors else f"退出码 {exit_code}"
        return self._failed(parsed, stderr, now)

    def _failed(self, parsed: Parsed, stderr: str, now: datetime) -> Parsed:
        text = joined(parsed.error, stderr)
        if QUOTA.search(text):
            after, at = TRY_AGAIN_IN.search(text), TRY_AGAIN_AT.search(text)
            resets = resets_after(after.group(1), now) if after is not None else \
                resets_at_clock(at.group(1), now) if at is not None else None
            parsed.status = CallStatus.QUOTA_EXHAUSTED
            parsed.rate_limits.append(quota_limit(self.name, window_for(resets, now), resets))
        elif AUTH.search(text):
            parsed.status = CallStatus.AUTH_FAILED
        elif REFUSAL.search(text):
            parsed.status = CallStatus.REFUSED
        return parsed


def tokens_of(usage: Mapping[str, Any] | None) -> Tokens:
    """codex 的 input_tokens 已含 cached_input_tokens，不再相加；不报缓存写入。"""
    if not usage:
        return Tokens()
    return Tokens(input=usage.get("input_tokens") or 0, output=usage.get("output_tokens") or 0,
                  cache_read=usage.get("cached_input_tokens") or 0)


def unwrap(command: str) -> str:
    """codex 把命令包成 `bash -lc '<命令>'`：取出里面的命令再按白名单检查。"""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if len(parts) == 3 and parts[0] in SHELLS and parts[1] in ("-lc", "-c"):
        return parts[2]
    return command
