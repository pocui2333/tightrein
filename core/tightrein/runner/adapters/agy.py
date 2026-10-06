"""Antigravity CLI(agy)适配器(architecture/02 2.5、2.6、2.8)。

- 无人值守：`agy --output-format stream-json [--json-schema <schema 文件>] -p <提示文本>`，工作目录即 workdir；
  提示只能作为 -p 的参数。事件为 init、step_update(user_input、agent_response、tool、system_message、finish)与
  最后一行 result(conversation_id、status、response、structured_output、usage、denied_actions、error)。
- 用量：agent_response 步骤完成时带本步 usage，按步骤求和；result.usage 是整个会话的累计(续接后包含此前各次调用)，
  只在 --output-format json 的单个对象中使用。input_tokens 不含缓存读取，统一用量的输入为两者之和。没有费用字段。
- 访问级别：只读为缺省权限模式加 --sandbox(终端写入被拦住)，可写为 --mode accept-edits；两者都不跳过权限。
  无人值守模式按 ~/.gemini/antigravity-cli/settings.json 的 permissions.allow 放行命令(`command(git grep)` 放行以它开头的
  命令，1.2.17 实测)，其余需要确认的写入与命令被自动拒绝(不挂起)，拒绝后本轮随即结束，result.denied_actions 记为一条
  error 事件。首次调用的提示末尾列出白名单中已放行的只读命令(READ_COMMANDS 与白名单的交集)，要求先用 git grep 定位、
  再打开命中的文件；`tightrein admin install` 为 agy 补齐这些命令。白名单里一条都没有时说明 shell 命令不可用。
- 联网：只读模式下 search_web 放行，读取网页(read_url_content)被拒绝并结束本轮(实测)；task.web 为真时提示末尾
  另说明只依据搜索摘要作答。
- 没有原生的轮数与费用上限，由核心按工具步骤计数、按价格表估算费用；提示末尾写明工具调用的上限，让 agy 自己留出
  输出结论的余量。推理强度为 --effort。
- 格式重试以 --conversation <会话 ID> 续接同一会话。
- 不支持交互会话与收尾调用：不能预先指定会话 ID，会话记录在工具自己的数据目录中。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.runner.adapters.base import (
    ENDED_COMPLETED,
    ENDED_ERROR,
    InvocationFiles,
    ParsedRun,
    RetryContext,
    SessionRef,
    json_lines,
    load_line,
    read_dirs,
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

SUCCESS = "SUCCESS"
TOOL_STEP = "tool"
RESPONSE_STEP = "agent_response"
FINISHED_STATES = frozenset({"DONE", "ERROR"})
ERROR_STATE = "ERROR"
DONE_STATE = "DONE"
SETTINGS_PATH = ".gemini/antigravity-cli/settings.json"  # 相对用户主目录
SETTINGS = Path.home() / SETTINGS_PATH
# 交给 agy 的只读命令：在 --sandbox 中不能写文件；按白名单的写法逐条放行(放行以它开头的命令)
READ_COMMANDS = ("git grep", "git ls-files", "git log", "git show", "git diff", "grep", "ls", "cat", "head", "tail",
                 "wc")
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
TURNS_NOTE = "工具调用最多 {turns} 次，超过后本轮被终止、结论作废：先搜索、再读命中的位置，留出输出结论的余量。\n"
WEB_NOTE = (
    "\n# 联网\n联网检索只能用 search_web。打开网页(read_url_content 等)会被自动拒绝，并立即结束本轮；"
    "只依据搜索结果的摘要作答，出处写摘要中的链接并注明来源站点。\n"
)
INTERACTIVE_UNSUPPORTED = ("agy 不支持交互会话：不能预先指定会话 ID，会话记录也不在本工具可读的位置。"
                           "请把 routes.fix.session 指向 claude 或 codex 的模型别名")


def usage_of(data: Mapping[str, Any] | None) -> Usage:
    """输入 token 为 input_tokens 与 cache_read_tokens 之和，后者计为 cachedInputTokens。"""
    if not data:
        return Usage()
    cached = data.get("cache_read_tokens")
    parts = [part for part in (data.get("input_tokens"), cached) if part is not None]
    return Usage(sum(parts) if parts else None, data.get("output_tokens"), cached)


def _denied(result: Mapping[str, Any]) -> str | None:
    actions = result.get("denied_actions") or []
    if not actions:
        return None
    names = "、".join(str(action.get("action")) for action in actions if isinstance(action, dict))
    return f"无人值守模式自动拒绝了需要确认的动作：{names}"


def _result_events(result: Mapping[str, Any], *, with_usage: bool) -> list[EventDraft]:
    drafts = [EventDraft(USAGE, SYSTEM, usage=usage_of(result.get("usage")))] if with_usage else []
    drafts.append(EventDraft(RESULT, ASSISTANT, text=result.get("response")))
    if result.get("status") != SUCCESS:
        drafts.append(EventDraft(ERROR, SYSTEM, text=f"{result.get('status')}：{result.get('error')}"))
    denied = _denied(result)
    if denied is not None:
        drafts.append(EventDraft(ERROR, SYSTEM, text=denied))
    return drafts


def _tool_events(step: Mapping[str, Any]) -> list[EventDraft]:
    info = step.get("tool_info") or {}
    call_id = str(step.get("step_index"))
    failed = step.get("state") == ERROR_STATE
    output = (info.get("error") or {}).get("message") if failed else info.get("output")
    return [EventDraft(TOOL_CALL, ASSISTANT, tool_name=step.get("tool_name") or info.get("name"),
                       tool_input=info.get("parameters"), tool_call_id=call_id),
            EventDraft(TOOL_RESULT, USER, tool_call_id=call_id,
                       tool_output=output if isinstance(output, str) or output is None else json.dumps(output),
                       is_error=failed)]


def _step_events(step: Mapping[str, Any]) -> list[EventDraft]:
    kind = step.get("step_type")
    if kind == TOOL_STEP:
        return _tool_events(step) if step.get("state") in FINISHED_STATES else []
    drafts = []
    if step.get("text_delta"):
        drafts.append(EventDraft(MESSAGE, ASSISTANT if kind == RESPONSE_STEP else SYSTEM, text=step["text_delta"]))
    if step.get("usage"):
        drafts.append(EventDraft(USAGE, SYSTEM, usage=usage_of(step["usage"])))
    if step.get("state") == ERROR_STATE:
        drafts.append(EventDraft(ERROR, SYSTEM, text=f"步骤 {step.get('step_index')}({kind})出错"))
    return drafts


def _is_whole_output(data: Mapping[str, Any]) -> bool:
    """--output-format json 的单个对象：没有 event 键，直接是 result 的内容。"""
    return "event" not in data and "status" in data


def allowed(settings: Path) -> tuple[str, ...]:
    """agy 白名单中 `command(<命令>)` 放行的命令；文件不存在或读不出时为空。"""
    try:
        entries = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    except (OSError, ValueError, KeyError, TypeError):
        return ()
    return tuple(item[len("command("):-1] for item in entries
                 if isinstance(item, str) and item.startswith("command(") and item.endswith(")"))


def read_commands(settings: Path) -> tuple[str, ...]:
    """READ_COMMANDS 中已在 agy 白名单里放行的。"""
    found = set(allowed(settings))
    return tuple(command for command in READ_COMMANDS if command in found)


def tool_note(commands: Sequence[str], max_turns: int | None) -> str:
    note = SEARCH_NOTE.format(commands="、".join(f"`{item}`" for item in commands)) if commands else SHELL_NOTE
    return note + (TURNS_NOTE.format(turns=max_turns) if max_turns else "")


class AgyAdapter:
    name = "agy"
    supports_schema = True
    supports_turn_limit = False
    supports_budget_limit = False
    supports_resume_by_id = True
    supports_image_input = True
    env_names: tuple[str, ...] = ()

    def __init__(self, settings: Path = SETTINGS) -> None:
        self.settings = settings

    def build(self, task: RunnerTask, files: InvocationFiles, *, executable: str, model: str | None,
              env: Mapping[str, str], retry: RetryContext | None, effort: str | None = None) -> Invocation:
        argv = [executable, *(("--sandbox",) if task.readonly else ("--mode", "accept-edits"))]
        if model is not None:
            argv += ["--model", model]
        if effort is not None:
            argv += ["--effort", effort]
        for directory in read_dirs(task):
            argv += ["--add-dir", directory]
        argv += ["--output-format", "stream-json"]
        if task.output_schema is not None:
            argv += ["--json-schema", str(files.schema)]
        if retry is not None and retry.session_id is not None:
            argv += ["--conversation", retry.session_id, "-p", retry.note]
        else:
            note = tool_note(read_commands(self.settings), task.limits.max_turns)
            argv += ["-p", files.prompt.read_text(encoding="utf-8") + note + (WEB_NOTE if task.web else "")]
        return Invocation(tuple(argv), task.workdir, env)

    def new_session_id(self) -> str | None:
        raise RunnerConfigError(INTERACTIVE_UNSUPPORTED)

    def build_interactive(self, task: RunnerTask, files: InvocationFiles, first_input: str, *, executable: str,
                          model: str | None, env: Mapping[str, str], session: SessionRef | None,
                          resume: bool, effort: str | None = None) -> Invocation:
        raise RunnerConfigError(INTERACTIVE_UNSUPPORTED)

    def build_finalize(self, task: RunnerTask, files: InvocationFiles, session: SessionRef, *, executable: str,
                       model: str | None, env: Mapping[str, str], effort: str | None = None) -> Invocation:
        raise RunnerConfigError(INTERACTIVE_UNSUPPORTED)

    def convert(self, line: str) -> list[EventDraft]:
        data = load_line(line)
        if data is None:
            return [unknown(line)]
        if _is_whole_output(data):
            return _result_events(data, with_usage=True)
        kind = data.get("event")
        if kind == "init":
            init = data.get("init") or {}
            return [EventDraft(SESSION_START, SYSTEM, text=f"model {init.get('model')}",
                               session_id=data.get("conversation_id"))]
        if kind == "step_update":
            return _step_events(data.get("step_update") or {})
        if kind == "result":
            return _result_events(data.get("result") or {}, with_usage=False)
        return [unknown(line)]

    def closing_events(self, parsed: ParsedRun) -> list[EventDraft]:
        return []

    def parse(self, raw_stdout: Path, exit_code: int) -> ParsedRun:
        records = json_lines(raw_stdout)
        if len(records) == 1 and _is_whole_output(records[0]):
            return self._parsed(records[0], usage_of(records[0].get("usage")), None, None, exit_code)
        session_id = None
        usage = Usage()
        turns = 0
        result: Mapping[str, Any] | None = None
        for data in records:
            kind = data.get("event")
            if kind == "init":
                session_id = data.get("conversation_id")
            elif kind == "step_update":
                step = data.get("step_update") or {}
                if step.get("step_type") == TOOL_STEP and step.get("state") in FINISHED_STATES:
                    turns += 1
                elif step.get("usage"):
                    usage = usage + usage_of(step["usage"])
            elif kind == "result":
                result = data.get("result") or {}
        if result is None:
            return ParsedRun(session_id, None, None, usage, turns, ENDED_ERROR, "输出中没有 result 事件")
        return self._parsed(result, usage, session_id, turns, exit_code)

    def _parsed(self, result: Mapping[str, Any], usage: Usage, session_id: str | None, turns: int | None,
                exit_code: int) -> ParsedRun:
        session_id = result.get("conversation_id") or session_id
        structured = result.get("structured_output")
        structured = structured if isinstance(structured, dict) else None
        if result.get("status") == SUCCESS and exit_code == 0:
            return ParsedRun(session_id, result.get("response"), structured, usage, turns, ENDED_COMPLETED)
        message = result.get("error") or f"{result.get('status')}，退出码 {exit_code}"
        return ParsedRun(session_id, result.get("response"), structured, usage, turns, ENDED_ERROR, message)

    # 交互会话(不支持)

    def locate_session(self, workdir: Path, started_at: datetime, session_id: str | None) -> SessionRef | None:
        return None

    def session_events(self, session: SessionRef) -> list[EventDraft]:
        return []
