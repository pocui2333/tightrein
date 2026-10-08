"""Antigravity CLI(agy)适配器；参数对照、已验证版本与不支持的项见 agy.md。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.agents.params import Access, CallParams, Model
from tightrein.agents.result import CallStatus
from tightrein.agents.tools import (
    NO_RESULT,
    NOTHING,
    LineEvent,
    Parsed,
    Resume,
    joined,
    json_records,
    load_line,
    quota_limit,
    read_dirs,
    resets_after,
    window_for,
    write_schema,
)
from tightrein.protocol.handoff import Tokens
from tightrein.protocol.process import Command

SUCCESS = "SUCCESS"
TOOL_STEP = "tool"
FINISHED_STATES = frozenset({"DONE", "ERROR"})
SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
NO_BROWSER_DIR = Path(tempfile.gettempdir()) / "tightrein-no-browser"
NO_BROWSER = "#!/bin/sh\n# tightrein：无人值守调用 agy 时不打开浏览器(登录凭据读取被中断时 agy 会转入浏览器登录)\nexit 1\n"
SHELL_NOTE = (
    "\n# 工具说明\n本次运行中 shell 命令会被自动拒绝，并立即结束本轮。读取、列目录、搜索与编辑文件请只用内置工具，"
    "不要调用 shell；需要运行的检查命令由调用方在之后执行。\n"
)
SEARCH_NOTE = (
    "\n# 工具说明\n本次运行可以执行这些只读命令：{commands}。其他命令会被自动拒绝，并立即结束本轮；需要运行的检查命令"
    "(测试、构建)由调用方在之后执行。每次只执行一条命令，不要用 `&&`、`||`、`;`、`|` 把多条命令连在一起"
    "(其中任何一条不在上面的列表里，整条都会被拒绝)。\n定位代码先用 `git grep -n <标识符或关键词>` 搜索(同一概念换几种命名)，再打开命中"
    "的文件；不要逐个打开文件浏览，同一个文件读过就不要重复打开。\n"
)
READ_NOTE = (
    "打开文件时用 view_file 的 StartLine、EndLine 只看命中行前后几十行，不要不带范围打开整个文件；同一段读过就不要再读。"
    "只读工作目录内的文件，不读工作目录以外的文件(包括 ~/.gemini 下的日志与任务记录)；命令输出太长时，换更具体的关键词"
    "或限定路径重新搜索。\n"
)
TURNS_NOTE = "工具调用最多 {turns} 次，超过后本轮被终止、结论作废：先搜索、再读命中的位置，留出输出结论的余量。\n"
WEB_NOTE = (
    "\n# 联网\n联网检索只能用 search_web。打开网页(read_url_content 等)会被自动拒绝，并立即结束本轮；"
    "只依据搜索结果的摘要作答，出处写摘要中的链接并注明来源站点。\n"
)
QUOTA = re.compile(r"individual quota reached|quota (?:has been )?reached", re.IGNORECASE)
RESETS_IN = re.compile(r"resets in\s+([\dhms]+)", re.IGNORECASE)
AUTH = re.compile(r"not logged in|unauthenticated|authentication (?:failed|required)|login required"
                  r"|invalid credentials|please (?:log ?in|sign in)", re.IGNORECASE)
REFUSAL = re.compile(r"blocked (?:due to|by) safety|safety (?:filter|block)|finish_?reason\W+safety"
                     r"|prohibited content", re.IGNORECASE)


class AgyAdapter:
    name = "agy"
    native_turns = False
    checks_commands = True  # 按 agy 自己的 permissions.allow 白名单放行，其余被自动拒绝
    env_names: tuple[str, ...] = ()

    def __init__(self, settings: Path = SETTINGS) -> None:
        self.settings = settings

    def env_values(self, params: CallParams) -> dict[str, str]:
        return {}

    def build(self, params: CallParams, model: Model, *, executable: str, env: dict[str, str],
              schema: dict[str, Any] | None, scratch: Path, resume: Resume | None) -> Command:
        # 都不用 --dangerously-skip-permissions：只读靠 --sandbox 拦住终端写入，可写只放开编辑
        argv = [executable, *(("--sandbox",) if params.access is Access.READ else ("--mode", "accept-edits"))]
        if model.model:
            argv += ["--model", model.model]
        if model.effort:
            argv += ["--effort", model.effort]
        for directory in read_dirs(params):
            argv += ["--add-dir", directory]
        argv += ["--output-format", "stream-json"]
        if schema is not None:
            argv += ["--json-schema", str(write_schema(schema, scratch))]
        if resume is not None:
            argv += ["--conversation", resume.session_id, "-p", resume.note]
        else:
            # 提示只能作为 -p 的参数(agy 不从标准输入读提示)
            permitted = set(allowed(self.settings))
            commands = [command for command in params.allowed_commands if command in permitted]
            note = tool_note(commands, params.limits.turns) + (WEB_NOTE if params.network else "")
            argv += ["-p", params.prompt + note]
        return Command(argv=tuple(argv), cwd=params.workdir, env=no_browser(env))

    def parse_line(self, line: str) -> LineEvent:
        data = load_line(line)
        if data is None or data.get("event") != "step_update":
            return NOTHING
        return _step_event(data.get("step_update") or {})

    def parse(self, stdout: str, stderr: str, exit_code: int | None, now: datetime) -> Parsed:
        records = json_records(stdout)
        if len(records) == 1 and _is_whole_output(records[0]):
            return self._parsed(records[0], tokens_of(records[0].get("usage")), None, None, stderr, exit_code, now)
        session_id = None
        tokens = Tokens()
        turns = 0
        result: Mapping[str, Any] | None = None
        for data in records:
            kind = data.get("event")
            if kind == "init":
                session_id = data.get("conversation_id")
            elif kind == "step_update":
                event = _step_event(data.get("step_update") or {})
                turns += event.tool_calls
                if event.tokens is not None:
                    tokens.add(event.tokens)
            elif kind == "result":
                result = data.get("result") or {}
        if result is None:
            parsed = Parsed(CallStatus.FAILED, session_id, tokens=tokens, turns=turns, error=NO_RESULT)
            return self._failed(parsed, stderr, now)
        # 续接后 result.usage 是整个会话的累计，只计本次调用：用各步骤的 usage 之和
        return self._parsed(result, tokens, session_id, turns, stderr, exit_code, now)

    def _parsed(self, result: Mapping[str, Any], tokens: Tokens, session_id: str | None, turns: int | None,
                stderr: str, exit_code: int | None, now: datetime) -> Parsed:
        structured = result.get("structured_output")
        parsed = Parsed(CallStatus.OK, result.get("conversation_id") or session_id, result.get("response"),
                        structured if isinstance(structured, dict) else None, tokens, None, turns)
        denied = _denied(result)
        if result.get("status") == SUCCESS and exit_code == 0:
            parsed.error = denied
            return parsed
        parsed.status = CallStatus.FAILED
        parsed.error = joined(result.get("error") or f"{result.get('status')}，退出码 {exit_code}", denied)
        return self._failed(parsed, stderr, now)

    def _failed(self, parsed: Parsed, stderr: str, now: datetime) -> Parsed:
        text = joined(parsed.error, stderr)
        if QUOTA.search(text):
            after = RESETS_IN.search(text)
            resets = resets_after(after.group(1), now) if after is not None else None
            parsed.status = CallStatus.QUOTA_EXHAUSTED
            parsed.rate_limits.append(quota_limit(self.name, window_for(resets, now), resets))
        elif AUTH.search(text):
            parsed.status = CallStatus.AUTH_FAILED
        elif REFUSAL.search(text):
            parsed.status = CallStatus.REFUSED
        return parsed


def tokens_of(usage: Mapping[str, Any] | None) -> Tokens:
    """agy 的 input_tokens 不含缓存读取：统一的输入为两者之和，缓存读取另记。agy 不报缓存写入。"""
    if not usage:
        return Tokens()
    cache_read = usage.get("cache_read_tokens") or 0
    return Tokens(input=(usage.get("input_tokens") or 0) + cache_read, output=usage.get("output_tokens") or 0,
                  cache_read=cache_read)


def no_browser(env: Mapping[str, str]) -> dict[str, str]:
    """PATH 最前面放只 `exit 1` 的 open、xdg-open：agy 读登录凭据失败(常见于被中断后)时直接失败，不弹浏览器登录页。"""
    NO_BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("open", "xdg-open"):
        target = NO_BROWSER_DIR / name
        if not target.is_file() or target.read_text(encoding="utf-8") != NO_BROWSER:
            target.write_text(NO_BROWSER, encoding="utf-8")
            target.chmod(0o755)
    path = env.get("PATH", "")
    return {**env, "PATH": os.pathsep.join(part for part in (str(NO_BROWSER_DIR), path) if part)}


def allowed(settings: Path) -> tuple[str, ...]:
    """agy 白名单中 `command(<命令>)` 放行的命令(放行以它开头的命令，1.2.17 实测)；文件不存在或读不出时为空。"""
    try:
        entries = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    except (OSError, ValueError, KeyError, TypeError):
        return ()
    return tuple(item[len("command("):-1] for item in entries
                 if isinstance(item, str) and item.startswith("command(") and item.endswith(")"))


def tool_note(commands: Sequence[str], turns: int | None) -> str:
    """原提示要求不调用 shell 时 agy 只能逐个打开文件，同一文件读 5 次、单次 340 万 token 超时：
    改为列出能用的只读命令、要求先 git grep 定位、每次只执行一条命令，并写明工具调用上限。
    view_file 不带行号范围时 agy 会把同一个大文件从头读七八遍、还去翻 ~/.gemini 下自己的任务日志，耗尽工具调用：
    另要求按行号范围读、只读工作目录内的文件(READ_NOTE)。"""
    note = SEARCH_NOTE.format(commands="、".join(f"`{item}`" for item in commands)) if commands else SHELL_NOTE
    return note + READ_NOTE + (TURNS_NOTE.format(turns=turns) if turns else "")


def _step_event(step: Mapping[str, Any]) -> LineEvent:
    """工具步骤结束(DONE 或 ERROR)算一次工具调用；agent_response 等步骤完成时带本步用量。工具步骤开始(ACTIVE)时
    就带出参数：读取越界在执行前即可发现。"""
    if step.get("step_type") == TOOL_STEP:
        info = step.get("tool_info") or {}
        inputs = ((str(info.get("name") or step.get("tool_name") or ""), info.get("parameters")),)
        return LineEvent(tool_calls=1 if step.get("state") in FINISHED_STATES else 0, tool_inputs=inputs)
    return LineEvent(tokens=tokens_of(step["usage"])) if step.get("usage") else NOTHING


def _denied(result: Mapping[str, Any]) -> str | None:
    """被自动拒绝的动作：拒绝后本轮随即结束，记进错误说明。"""
    actions = result.get("denied_actions") or []
    names = "、".join(str(action.get("action")) for action in actions if isinstance(action, dict))
    return f"无人值守模式自动拒绝了需要确认的动作：{names}" if names else None


def _is_whole_output(data: Mapping[str, Any]) -> bool:
    """--output-format json 的单个对象：没有 event 键，直接是 result 的内容。"""
    return "event" not in data and "status" in data
