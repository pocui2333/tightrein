"""Claude Code 适配器；参数对照、已验证版本与不支持的项见 claude.md。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tightrein.agents.params import Access, CallParams, Model
from tightrein.agents.result import CallStatus, RateLimit
from tightrein.agents.tools import (
    NO_RESULT,
    NOTHING,
    CallConfigError,
    LineEvent,
    Parsed,
    Resume,
    iso,
    joined,
    json_records,
    load_line,
    quota_limit,
    read_dirs,
    resets_at_clock,
)
from tightrein.protocol.handoff import Tokens
from tightrein.protocol.process import Command

READ_TOOLS = ("Read", "Grep", "Glob")
WRITE_TOOLS = ("Edit", "Write")
WEB_TOOLS = ("WebSearch", "WebFetch")
SHELL = "Bash"
SUCCESS = "success"
ENDINGS = {"error_max_turns": CallStatus.TURN_LIMIT, "error_max_budget_usd": CallStatus.BUDGET_LIMIT}
# rate_limit_event 的 rateLimitType 与 status → 统一的额度窗口与状态
WINDOWS = {"five_hour": "five_hour", "seven_day": "weekly", "seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}
LIMIT_STATES = {"allowed": "allowed", "allowed_warning": "warning", "rejected": "rejected"}
MAX_OUTPUT_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
SCHEMA_DIALECT_KEY = "$schema"
SESSION_LIMIT = re.compile(r"hit your (?P<kind>[\w ]*?)\s*limit\b[^\n]*?\bresets\s+(?P<when>[^\n\"]+)", re.IGNORECASE)
LEGACY_LIMIT = re.compile(r"usage limit reached\|(?P<epoch>\d{9,})", re.IGNORECASE)
AUTH = re.compile(r"invalid api key|please run /login|not logged in|oauth token (?:has )?expired|authentication_error"
                  r"|api error: 401", re.IGNORECASE)
REFUSAL = re.compile(r"violates? (?:our|the|anthropic's) usage polic|unable to respond to this request", re.IGNORECASE)
DROPPED_FROM_TARGET = ("$schema", "$id", "title")


class ClaudeAdapter:
    name = "claude"
    native_turns = True
    checks_commands = True
    env_names: tuple[str, ...] = ("CLAUDE_CONFIG_DIR",)

    def env_values(self, params: CallParams) -> dict[str, str]:
        # claude 没有单次输出上限的命令行参数，用它认的环境变量
        return {MAX_OUTPUT_ENV: str(params.limits.output_tokens)}

    def build(self, params: CallParams, model: Model, *, executable: str, env: dict[str, str],
              schema: dict[str, Any] | None, scratch: Path, resume: Resume | None) -> Command:
        if resume is not None and resume.finalize:
            return self._finalize(params, model, executable, env, schema, resume)
        argv = [executable, "-p", "--output-format", "stream-json", "--verbose", *_permissions(params),
                "--max-turns", str(params.limits.turns), *_model_options(model)]
        if schema is not None:
            argv += ["--json-schema", cli_schema(schema)]
        if resume is not None:
            argv += ["--resume", resume.session_id]
        # 提示经标准输入传入，不受命令行长度限制
        stdin = params.prompt if resume is None else resume.note
        return Command(argv=tuple(argv), cwd=params.workdir, env=env, stdin=stdin)

    def _finalize(self, params: CallParams, model: Model, executable: str, env: dict[str, str],
                  schema: dict[str, Any] | None, resume: Resume) -> Command:
        argv = [executable, "-p", "--resume", resume.session_id, "--output-format", "json"]
        if schema is not None:
            argv += ["--json-schema", cli_schema(schema)]
        argv += _model_options(model)
        return Command(argv=tuple(argv), cwd=params.workdir, env=env, stdin=resume.note)

    def parse_line(self, line: str) -> LineEvent:
        data = load_line(line)
        if data is None:
            return NOTHING
        kind = data.get("type")
        if kind == "rate_limit_event":
            return LineEvent(rate_limit=rate_limit_of(data.get("rate_limit_info") or {}))
        if kind != "assistant":
            return NOTHING
        message = data.get("message") or {}
        content = message.get("content")
        uses = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"] \
            if isinstance(content, list) else []
        usage = message.get("usage")
        return LineEvent(tool_calls=len(uses), tokens=tokens_of(usage) if usage else None, usage_id=message.get("id"),
                         tool_inputs=tuple((str(block.get("name") or ""), block.get("input")) for block in uses))

    def parse(self, stdout: str, stderr: str, exit_code: int | None, now: datetime) -> Parsed:
        records = json_records(stdout)
        session_id = next((item.get("session_id") for item in records if item.get("type") == "system"), None)
        limits = [rate_limit_of(item.get("rate_limit_info") or {}) for item in records
                  if item.get("type") == "rate_limit_event"]
        results = [item for item in records if item.get("type") == "result"]
        if not results:
            return self._failed(Parsed(CallStatus.FAILED, session_id, error=NO_RESULT, rate_limits=limits),
                                stderr, now)
        result = results[-1]
        subtype = result.get("subtype")
        parsed = Parsed(
            CallStatus.OK, result.get("session_id") or session_id, result.get("result"),
            result.get("structured_output") if isinstance(result.get("structured_output"), dict) else None,
            tokens_of(result.get("usage")), result.get("total_cost_usd"), result.get("num_turns"), None, limits,
        )
        if result.get("stop_reason") == "refusal":
            parsed.status, parsed.error = CallStatus.REFUSED, "模型拒绝回答(stop_reason refusal)"
            return parsed
        if subtype == SUCCESS and not result.get("is_error") and exit_code == 0:
            return parsed
        parsed.error = f"{subtype}：{result.get('result')}"
        if subtype in ENDINGS:
            parsed.status = ENDINGS[subtype]
            return parsed
        parsed.status = CallStatus.FAILED
        return self._failed(parsed, stderr, now)

    def _failed(self, parsed: Parsed, stderr: str, now: datetime) -> Parsed:
        """按报错识别额度用完、认证失败与被拒绝；rate_limit_event 已是 rejected 的也是额度用完。"""
        text = joined(parsed.error, stderr)
        session = SESSION_LIMIT.search(text)
        legacy = LEGACY_LIMIT.search(text)
        if session is not None:
            parsed.status = CallStatus.QUOTA_EXHAUSTED
            parsed.rate_limits.append(quota_limit(self.name, _window(session.group("kind")),
                                                  resets_at_clock(session.group("when"), now)))
        elif legacy is not None:
            parsed.status = CallStatus.QUOTA_EXHAUSTED
            resets = datetime.fromtimestamp(int(legacy.group("epoch")), UTC)
            parsed.rate_limits.append(quota_limit(self.name, "five_hour", resets))
        elif any(limit.status == "rejected" for limit in parsed.rate_limits):
            parsed.status = CallStatus.QUOTA_EXHAUSTED
        elif AUTH.search(text):
            parsed.status = CallStatus.AUTH_FAILED
        elif REFUSAL.search(text):
            parsed.status = CallStatus.REFUSED
        return parsed


def tokens_of(usage: Mapping[str, Any] | None) -> Tokens:
    """输入 = 未缓存 + 写入缓存 + 读取缓存；读取缓存另记(每个 Issue 的用量上限按 1/10 计)。"""
    if not usage:
        return Tokens()
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    return Tokens(input=(usage.get("input_tokens") or 0) + cache_write + cache_read,
                  output=usage.get("output_tokens") or 0, cache_read=cache_read, cache_write=cache_write)


def rate_limit_of(info: Mapping[str, Any]) -> RateLimit:
    kind = info.get("rateLimitType") or "five_hour"
    resets = info.get("resetsAt")
    used = info.get("utilization")
    if isinstance(used, (int, float)) and used > 1:  # 有的版本给百分数
        used = used / 100
    return RateLimit(
        tool="claude", window=WINDOWS.get(kind, kind), status=LIMIT_STATES.get(str(info.get("status")), "allowed"),
        used_ratio=float(used) if isinstance(used, (int, float)) else None,
        resets_at=iso(datetime.fromtimestamp(resets, UTC)) if isinstance(resets, (int, float)) else None,
    )


def cli_schema(schema: dict[str, Any]) -> str:
    """传给 --json-schema 的文本：CLI 解析不了 `$ref`，先全部内联；它的校验器也不认 2020-12 的 `$schema` 声明，去掉。"""
    inlined = inline(schema)
    inlined.pop(SCHEMA_DIALECT_KEY, None)
    return json.dumps(inlined, ensure_ascii=False)


def inline(schema: dict[str, Any]) -> dict[str, Any]:
    """展开文档内的全部 `$ref`(`#`、`#/$defs/…`)；递归 schema 无法展开，视为配置错误。"""
    result = _inline(schema, schema, ())
    if not isinstance(result, dict):
        raise CallConfigError("schema 顶层必须是对象")
    return result


def _inline(node: Any, root: dict[str, Any], trail: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_inline(item, root, trail) for item in node]
    if not isinstance(node, dict):
        return node
    result = {key: _inline(value, root, trail) for key, value in node.items() if key not in ("$ref", "$defs")}
    reference = node.get("$ref")
    if reference is None:
        return result
    if not isinstance(reference, str) or not reference.startswith("#"):
        raise CallConfigError(f"schema 只能引用本文档内的定义：{reference}")
    if reference in trail:
        raise CallConfigError(f"schema 有循环引用，无法内联：{reference}")
    target = _inline(_pointer(root, reference), root, (*trail, reference))
    target = {key: value for key, value in target.items() if key not in DROPPED_FROM_TARGET}
    siblings = {key: value for key, value in result.items() if key not in DROPPED_FROM_TARGET}
    if not siblings:
        return {**{key: result[key] for key in ("$schema", "title") if key in result}, **target}
    return {**result, "allOf": [*result.get("allOf", []), target]}


def _pointer(root: dict[str, Any], reference: str) -> dict[str, Any]:
    node: Any = root
    for part in reference[1:].lstrip("/").split("/") if reference != "#" else ():
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise CallConfigError(f"schema 引用无法解析：{reference}")
        node = node[part]
    if not isinstance(node, dict):
        raise CallConfigError(f"schema 引用的不是对象：{reference}")
    return node


def _permissions(params: CallParams) -> list[str]:
    """--tools 与 --allowedTools 各用逗号连成一个参数；放行命令写成 `Bash(<命令前缀> *)`。"""
    writable = params.access is Access.WRITE
    tools = [*READ_TOOLS, SHELL, *(WRITE_TOOLS if writable else ()), *(WEB_TOOLS if params.network else ())]
    allowed = [*READ_TOOLS, *(f"{SHELL}({command} *)" for command in params.allowed_commands),
               *(WEB_TOOLS if params.network else ())]
    argv = ["--tools", ",".join(tools), "--allowedTools", ",".join(allowed),
            "--permission-mode", "acceptEdits" if writable else "dontAsk"]
    for directory in read_dirs(params):
        argv += ["--add-dir", directory]
    return argv


def _model_options(model: Model) -> list[str]:
    argv = ["--model", model.model] if model.model else []
    return argv + (["--effort", model.effort] if model.effort else [])


def _window(kind: str) -> str:
    kind = kind.lower()
    if "opus" in kind:
        return "opus"
    if "sonnet" in kind:
        return "sonnet"
    if "week" in kind:
        return "weekly"
    return "five_hour"
