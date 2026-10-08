"""调用 AI 的统一入口(agents/README.md)：所有模型调用只走 call()。

一次 call：
1. 开始时写 started 标记(watch 据此显示正在调用与已跑多久)；
2. 依赖熔断时改走备用模型；该工具额度用完(到重置时间前)或该对象已到用量上限时不启动；
3. 启动前检查工作目录中未跟踪(含被忽略)的凭据文件与仓库本地配置中的凭据，有就不启动(boundary)；
   调用前记边界快照：只读步骤记工作目录，可写步骤记主仓库(git 状态)与 worktree 之外 agent 不可写的路径(文件快照)；
4. 适配器生成命令，经 protocol.process 运行；逐行数 tool-call、累计 token，工具原生不支持的上限由程序兜底杀进程；
   rate_limit_event 为 rejected 时立即停下；工具调用读了隐藏目录或凭据文件即越界并停下；
5. 每次工具调用后比对边界快照，有越界即停(不做格式重试)；中断等异常时也比对并记一条；
6. 解析输出，提取结构化结果按 schema 校验；
7. 不成功时由 protocol.limits.decide 决定：临时错误退避重试同一次调用、格式不符续接同一会话带每条错误与原输出
   重试、被拒绝或不可用换备用模型、额度用完全部停、其余停下；
8. 无论怎样结束都在 finally 中把用量累计到对象、额度信号写入 Quota；失败或调试时保存 prompt 与 raw。
所有失败都以 CallResult 返回；只有编程或配置错误(未知工具、工具不支持的参数组合)才抛 CallConfigError。
"""

from __future__ import annotations

import json
import random
import re
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tightrein.agents.params import Access, CallParams, Model
from tightrein.agents.result import CallResult, CallStatus, RateLimit
from tightrein.agents.tools import FINALIZE_PROMPT, Adapter, CallConfigError, Parsed, Resume
from tightrein.agents.tools.agy import AgyAdapter
from tightrein.agents.tools.claude import ClaudeAdapter
from tightrein.agents.tools.codex import CodexAdapter
from tightrein.agents.tools.replay import NAME as REPLAY
from tightrein.agents.tools.replay import ReplayAdapter
from tightrein.protocol.boundaries import command_allowed, hidden_reads
from tightrein.protocol.handoff import Tokens, schema_errors
from tightrein.protocol.limits import Action, Breaker, backoff_s, decide
from tightrein.protocol.naming import STAGES, Clock, FileName, format_iso, step_sequence
from tightrein.protocol.process import OVERFLOW, Outcome, ProcessRunner
from tightrein.protocol.records import EventLog
from tightrein.protocol.resources import IssueBudget, Quota, Slots
from tightrein.protocol.security import (
    FileState,
    Redactor,
    SnapshotFailed,
    TreeSnapshot,
    changed_files,
    child_env,
    compare,
    credentials_present,
    file_snapshot,
    snapshot,
)
from tightrein.settings.load import MissingSetting, Settings
from tightrein.store.files.atomic import write_text
from tightrein.store.files.json import write_json
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

RETRY_OUTPUT_CHARS = 8000  # 重试说明中附上的原输出
TOOL_OUTPUT_CHARS = 16384  # raw 中单个字符串值的上限
TRUNCATED = "\n...(已截断)"
NO_JSON = "/：输出中没有可解析的 JSON 对象"
NOT_OBJECT = "/：结果须为 JSON 对象"
MILLION = 1_000_000
OVERFLOW_REASON = "标准输出超过上限，已被终止"
OVERFLOW_NOTE = "{hint}。请只输出符合 schema 的结论，大段内容写到文件后在输出中以路径引用。"
PROGRESS_S = 5.0  # 调用进行中更新 started 标记的最短间隔
# 进程被程序终止的原因(protocol.process 的 stopped_by 与本模块 on_line 返回的原因) → 统一状态
STOPPED = {
    "timeout": CallStatus.TIMEOUT, "idle": CallStatus.TIMEOUT, "overflow": CallStatus.SCHEMA_INVALID,
    "turns": CallStatus.TURN_LIMIT, "output_tokens": CallStatus.BUDGET_LIMIT, "issue_tokens": CallStatus.BUDGET_LIMIT,
    "quota": CallStatus.QUOTA_EXHAUSTED, "command": CallStatus.BOUNDARY, "hidden_read": CallStatus.BOUNDARY,
}
DEPENDENCY_FAILURES = frozenset({CallStatus.TRANSIENT, CallStatus.UNAVAILABLE})
NOT_COUNTED = frozenset({CallStatus.TIMEOUT})  # 说明不了工具本身是否可用
OVERLOADED = re.compile(r"\b529\b|overloaded", re.IGNORECASE)
RETRY_AFTER = re.compile(r"retry[- _]after\D{0,3}(\d+(?:\.\d+)?)", re.IGNORECASE)
CODE_BLOCK = re.compile(r"`{3}json[ \t]*\n(.*?)`{3}", re.DOTALL)
# agent 不得读取的家目录下的凭据位置(读了即越界)；工作区的 data/(其他对象的记录、原始输出)另由 _hidden 加上
HIDDEN_HOME = (".ssh", ".aws", ".gnupg", ".docker", ".kube", ".config/gh", ".netrc", ".git-credentials")


def default_adapters() -> dict[str, Adapter]:
    return {"claude": ClaudeAdapter(), "agy": AgyAdapter(), "codex": CodexAdapter()}


@dataclass
class AgentContext:
    settings: Settings
    layout: WorkspaceLayout
    conn: sqlite3.Connection
    clock: Clock
    runner: ProcessRunner
    redactor: Redactor
    events: EventLog
    environ: Mapping[str, str]
    breaker: Breaker
    quota: Quota
    budget: IssueBudget
    slots: Slots | None = None
    replay: ReplayAdapter | None = None  # 给出时全部调用都回放录制
    adapters: Mapping[str, Adapter] = field(default_factory=default_adapters)
    debug: bool = False  # 成功的调用也保存 prompt 与 raw
    sleep: Callable[[float], None] = time.sleep
    random: Callable[[], float] = random.random
    tool: ToolLayout = field(default_factory=ToolLayout.discover)  # tightrein 自身：可写步骤前后比对它的代码与配置
    # 本次运行中被拒绝过的(调用点、模型别名)：之后同一调用点直接走备用模型，不再每次先被拒一遍
    refused: set[tuple[str, str]] = field(default_factory=set)


def params_for(point: str, *, settings: Settings, run: str, subject: str | None, prompt: str,
               schema: dict[str, Any] | None, workdir: Path, conditions: tuple[str, ...] = (),
               access: Access | None = None, allowed_commands: Sequence[str] = (), read_paths: Sequence[Path] = (),
               resume_session: str | None = None, round: int | None = None, prompt_hash: str | None = None,
               finalize: bool = False) -> CallParams:
    """按调用点从 settings 取模型、备用模型、权限与上限，拼成统一参数。只读命令表(boundaries.readCommands)总是放行。"""
    model = settings.model_for(point, conditions)
    fallback = settings.fallback_for(point)
    commands = list(settings.get("boundaries.readCommands"))
    commands += [command for command in allowed_commands if command not in commands]
    return CallParams(
        point=point, run=run, subject=subject, round=round, model=model,
        fallback=None if fallback is None or fallback == model else fallback,
        prompt=prompt, schema=schema, workdir=workdir, read_paths=tuple(read_paths),
        access=access or Access(settings.control(point, "access")), allowed_commands=tuple(commands),
        network=bool(settings.control(point, "network")), limits=settings.limits_for(point),
        resume_session=resume_session, finalize=finalize,
        language=settings.project.language if settings.project is not None else "zh", prompt_hash=prompt_hash,
    )


def call(params: CallParams, context: AgentContext) -> CallResult:
    model, fallback_used, breaker_open = _choose(params, context)
    adapter = _adapter(model, context)
    marker = _file(params, context.layout, "started", "json")
    started = format_iso(context.clock.now())
    _mark(marker, params, model, started, None, context.clock)
    run = _Run(model, progress=_progress(marker, params, model, started, context.clock))
    if fallback_used and (params.point, params.model.alias) in context.refused:
        run.fallback_from = {"model": params.model.alias, "status": CallStatus.REFUSED.value}
    blocked = _blocked(params, model, context, breaker_open)
    if blocked is not None:
        return _finish(params, context, blocked, marker, started, run)
    try:
        present = _credentials(params, context)
    except SnapshotFailed as error:
        present = [f"snapshot：无法检查工作目录，不启动：{error}"]
    if present:
        refused = CallResult(CallStatus.BOUNDARY, model.tool, model.model, attempts=0, error="；".join(present),
                             violations=present)
        return _finish(params, context, refused, marker, started, run)
    with _slot(context):
        try:
            before = _snapshots(params, context)
        except SnapshotFailed as error:
            # 调用前的快照取不到，就无法判断调用后有没有越界：不启动，以结果返回(所有失败都不向上抛)
            refused = CallResult(CallStatus.BOUNDARY, model.tool, model.model, attempts=0,
                                 error=f"无法取调用前的快照，不启动：{error}")
            return _finish(params, context, refused, marker, started, run)
        try:
            result = _attempts(params, context, adapter, model, fallback_used, run, before)
        except BaseException:
            # 中断等异常也做调用后检查：越界要留下记录，异常照常抛出
            violations = _compare(before, context)
            if violations:
                _emit(context, params, "effect", "调用被中断，且有越界：" + "；".join(violations))
            raise
        finally:
            if params.subject is not None:
                context.budget.add(params.subject, run.tokens)
            context.quota.update(run.rate_limits)
    return _finish(params, context, result, marker, started, run)


# 选模型与启动前检查


def _choose(params: CallParams, context: AgentContext) -> tuple[Model, bool, bool]:
    """返回(模型、是否已换备用、是否熔断中且无备用)。依赖熔断期间改走备用模型(备用模型的工具未熔断时)。
    breaker.allow 在半开时会放出唯一一次试探，所以每个工具只问一次。"""
    model = params.model
    if context.replay is not None:
        return model, False, False
    fallback = params.fallback
    if (params.point, model.alias) in context.refused and fallback is not None \
            and context.breaker.allow(fallback.tool):
        _emit(context, params, "decision", f"{model.alias} 本次运行在这个调用点被拒绝过，直接用备用模型 {fallback.alias}")
        return fallback, True, False
    if context.breaker.allow(model.tool):
        return model, False, False
    if fallback is not None and fallback.tool != model.tool and context.breaker.allow(fallback.tool):
        _emit(context, params, "decision", f"{model.tool} 依赖熔断中，改用备用模型 {fallback.alias}")
        return fallback, True, False
    return model, False, True


def _adapter(model: Model, context: AgentContext) -> Adapter:
    if context.replay is not None:
        return context.replay
    if model.tool == REPLAY:
        raise CallConfigError(f"模型别名 {model.alias} 指向回放，但没有给出录制集")
    if model.tool not in context.adapters:
        raise CallConfigError(f"没有工具 {model.tool} 的适配器，可用的有 {'、'.join(sorted(context.adapters))}")
    return context.adapters[model.tool]


def _blocked(params: CallParams, model: Model, context: AgentContext, breaker_open: bool) -> CallResult | None:
    halted = context.quota.halted_until(model.tool)
    if halted is not None:
        return CallResult(CallStatus.QUOTA_EXHAUSTED, model.tool, model.model, attempts=0,
                          error=f"{model.tool} 的订阅额度已用完，{format_iso(halted)} 重置")
    if params.subject is not None and context.budget.exceeded(params.subject):
        return CallResult(CallStatus.BUDGET_LIMIT, model.tool, model.model, attempts=0,
                          error=f"{params.subject} 的用量已达上限 {context.budget.limit:.0f} token")
    if breaker_open:
        return CallResult(CallStatus.UNAVAILABLE, model.tool, model.model, attempts=0,
                          error=f"{model.tool} 依赖熔断中，且没有可用的备用模型")
    return None


@contextmanager
def _slot(context: AgentContext) -> Iterator[None]:
    if context.slots is None:
        yield
        return
    with context.slots.hold("modelCalls"):
        yield


# 调用与重试


@dataclass
class _Attempt:
    parsed: Parsed
    status: CallStatus
    error: str | None
    outcome: Outcome | None
    violations: list[str] = field(default_factory=list)


@dataclass
class _Run:
    """一次 call 中全部工具调用的累计。"""

    model: Model
    tokens: Tokens = field(default_factory=Tokens)
    cost_usd: float | None = None
    cost_estimated: bool = False
    duration_ms: int = 0
    turns: int = 0
    attempts: int = 0
    session_id: str | None = None
    rate_limits: list[RateLimit] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    progress: Callable[[int, Tokens], None] | None = None  # 调用进行中更新 started 标记(watch 显示实时轮数与 token)
    fallback_from: dict[str, str] | None = None  # 换了备用模型时：原模型别名与它的结束状态(复盘据此记录)

    def add(self, attempt: _Attempt, model: Model) -> None:
        parsed = attempt.parsed
        self.attempts += 1
        self.model = model
        self.tokens.add(parsed.tokens)
        estimated = parsed.cost_usd is None
        cost = _estimate(parsed.tokens, model) if estimated else parsed.cost_usd
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + cost
            self.cost_estimated = self.cost_estimated or estimated
        self.turns += parsed.turns or 0
        self.session_id = parsed.session_id or self.session_id
        self.rate_limits += parsed.rate_limits
        if attempt.outcome is not None:
            self.duration_ms += attempt.outcome.duration_ms
            self.raw.append(_separator(self.attempts, model, attempt.outcome))
            self.raw += attempt.outcome.stdout.splitlines()

    def result(self, status: CallStatus, *, output: dict[str, Any] | None = None, text: str | None = None,
               error: str | None = None, violations: Sequence[str] = ()) -> CallResult:
        return CallResult(
            status=status, tool=self.model.tool, model=self.model.model, output=output, text=text, error=error,
            tokens=self.tokens, cost_usd=self.cost_usd, cost_estimated=self.cost_estimated,
            duration_ms=self.duration_ms, turns=self.turns, attempts=self.attempts,
            retries=max(self.attempts - 1, 0), session_id=self.session_id, rate_limits=list(self.rate_limits),
            violations=list(violations),
        )


def _attempts(params: CallParams, context: AgentContext, adapter: Adapter, model: Model, fallback_used: bool,
              run: _Run, before: _Before) -> CallResult:
    current = params
    resume = None if params.resume_session is None else Resume(params.resume_session, params.prompt)
    finalizing = False
    seen: Counter[CallStatus] = Counter()
    while True:
        # 要补要结构化结果的调用：先不带 schema 做完事，再续接同一会话只要 JSON
        schema = None if params.finalize and not finalizing else params.schema
        attempt = _invoke(current, context, adapter, model, schema, resume, run)
        run.add(attempt, model)
        # 每次工具调用后都比对边界；越界即停，不再做格式重试或换模型
        violations = attempt.violations + _compare(before, context)
        if violations:
            return run.result(CallStatus.BOUNDARY, error="；".join(violations), violations=violations)
        status, error, output = attempt.status, attempt.error, None
        # 输出超限也是格式不符(协议错误)：续接同一会话，提示大结果写文件、以路径引用
        note = OVERFLOW_NOTE.format(hint=error) if status is CallStatus.SCHEMA_INVALID and error else ""
        if status is CallStatus.OK and params.finalize and not finalizing and params.schema is not None:
            if run.session_id is not None:
                finalizing, resume = True, Resume(run.session_id, FINALIZE_PROMPT, finalize=True)
                continue
            status, error = CallStatus.FAILED, "工具没有给出会话编号，无法补要结构化结果"
        if status is CallStatus.OK and schema is not None:
            output, errors = checked_output(attempt.parsed, schema)
            if errors:
                status, error = CallStatus.SCHEMA_INVALID, "；".join(errors)
                note = retry_note(raw_output(attempt.parsed), errors)
        _record_dependency(context, model.tool, status)
        if status is CallStatus.OK:
            return run.result(status, output=output, text=attempt.parsed.text)
        seen[status] += 1
        action = decide(status, seen[status], fallback_used=fallback_used, settings=context.settings)
        if action is Action.RETRY:
            text = error or ""
            retry_after = RETRY_AFTER.search(text)
            context.sleep(backoff_s(seen[status], retry_after_s=float(retry_after.group(1)) if retry_after else None,
                                    overloaded=bool(OVERLOADED.search(text)), settings=context.settings,
                                    random=context.random))
            continue
        if action is Action.RESUME_WITH_REASON:
            if run.session_id is not None:
                resume = Resume(run.session_id, note, finalize=finalizing)
            else:  # 拿不到会话编号就新开一次，把说明附在提示末尾
                resume, current = None, replace(params, prompt=f"{params.prompt}\n\n{note}")
            continue
        if action is Action.FALLBACK and params.fallback is not None and params.fallback != model:
            _emit(context, params, "decision", f"{model.alias} {status.value}，换备用模型 {params.fallback.alias}")
            if status is CallStatus.REFUSED:
                context.refused.add((params.point, model.alias))
            run.fallback_from = {"model": model.alias, "status": status.value}
            model, fallback_used = params.fallback, True
            adapter = _adapter(model, context)
            resume, current, finalizing = None, params, False
            run.session_id = None
            continue
        if action is Action.HALT_ALL:
            _emit(context, params, "decision", f"{model.tool} 的订阅额度用完，全部停下：{error}")
        return run.result(status, error=error)


def _invoke(params: CallParams, context: AgentContext, adapter: Adapter, model: Model,
            schema: dict[str, Any] | None, resume: Resume | None, run: _Run) -> _Attempt:
    executable = model.tool if adapter is context.replay else _executable(model.tool, context)
    if executable is None:
        missing = f"找不到 {model.tool} 的可执行文件：在 settings 的 tools.{model.tool}.path 配置路径，或安装后确认已登录"
        return _Attempt(Parsed(CallStatus.UNAVAILABLE, error=missing), CallStatus.UNAVAILABLE, missing, None)
    env = child_env(context.environ, extra_allowed=adapter.env_names, set_values=adapter.env_values(params),
                    read_only=params.access is Access.READ)
    removed = sorted(set(context.environ) - set(env))
    if removed and run.attempts == 0:
        # 只记被去掉的变量名，不记值(要保留的设计：凭据不进任何记录)
        _emit(context, params, "action", "子进程环境去掉了：" + "、".join(removed))
    watch = _Watch(params, adapter, context, run)
    runner: ProcessRunner = context.replay if adapter is context.replay and context.replay is not None \
        else context.runner
    with tempfile.TemporaryDirectory(prefix="tightrein-call-") as scratch:
        command = adapter.build(params, model, executable=executable, env=env, schema=schema, scratch=Path(scratch),
                                resume=resume)
        outcome = runner.run(replace(command, timeout_s=params.limits.timeout_s, idle_s=params.limits.idle_s,
                                     on_line=watch.on_line))
    if outcome.start_error is not None:
        parsed = Parsed(CallStatus.UNAVAILABLE, error=outcome.start_error)
        return _Attempt(parsed, CallStatus.UNAVAILABLE, outcome.start_error, outcome)
    parsed = adapter.parse(outcome.stdout, outcome.stderr_tail, outcome.exit_code, context.clock.now())
    parsed.turns = parsed.turns if parsed.turns is not None else watch.turns
    if outcome.stopped_by is not None:
        if parsed.tokens == Tokens():  # 被杀时工具来不及报总用量，用逐行累计的
            parsed.tokens = watch.tokens
        status = STOPPED.get(outcome.stopped_by, CallStatus.FAILED)
        reason = watch.reason or (OVERFLOW_REASON if outcome.stopped_by == OVERFLOW
                                  else f"进程被终止：{outcome.stopped_by}")
        return _Attempt(parsed, status, reason, outcome, watch.violations)
    error = None if parsed.status is CallStatus.OK else \
        "\n".join(part for part in (f"退出码 {outcome.exit_code}：{parsed.error or ''}", outcome.stderr_tail) if part)
    status = CallStatus.TRANSIENT if parsed.status is CallStatus.FAILED and _transient(error or "", context.settings) \
        else parsed.status
    return _Attempt(parsed, status, error, outcome)


class _Watch:
    """逐行观察工具输出：工具原生不支持的上限由程序兜底，超了就返回终止原因。"""

    def __init__(self, params: CallParams, adapter: Adapter, context: AgentContext, run: _Run) -> None:
        self.turn_limit = None if adapter.native_turns else params.limits.turns
        self.output_limit = params.limits.output_tokens
        self.allowed = params.allowed_commands
        self.check_commands = not adapter.checks_commands
        self.adapter = adapter
        self.run = run
        weight = float(context.settings.get("resources.cacheReadWeight"))
        self.weight = weight
        # 本次调用还能用多少：对象的余量减去本次 call 中前几次工具调用已经用掉的
        self.room = None if params.subject is None else \
            context.budget.remaining(params.subject) - run.tokens.weighted(weight)
        self.turns = 0
        self.tokens = Tokens()
        self.counted: set[str] = set()
        self.reason: str | None = None
        self.violations: list[str] = []
        self.workdir = params.workdir
        self.hidden = _hidden(params, context)
        self.readable = (params.workdir, *params.read_paths)
        self.credential_patterns = tuple(context.settings.get("boundaries.protected.forbidden"))
        home = context.environ.get("HOME")
        self.home = Path(home) if home else None

    def on_line(self, line: str) -> str | None:
        event = self.adapter.parse_line(line)
        if event.rate_limit is not None and event.rate_limit.status == "rejected":
            return self._stop("quota", f"{event.rate_limit.tool} 的 {event.rate_limit.window} 额度已用完")
        self.turns += event.tool_calls
        if self.turn_limit is not None and self.turns > self.turn_limit:
            return self._stop("turns", f"工具调用超过 {self.turn_limit} 次")
        for command in event.commands if self.check_commands else ():
            if not command_allowed(command, self.allowed):
                self.violations.append(f"command：不在白名单的命令 {command}")
                return self._stop("command", f"执行了不在白名单的命令：{command}")
        if event.tool_inputs:
            reads = hidden_reads(event.tool_inputs, workdir=self.workdir, hidden=self.hidden,
                                 credential_patterns=self.credential_patterns, allowed=self.readable, home=self.home)
            if reads:
                self.violations += reads
                return self._stop("hidden_read", "；".join(reads))
        if event.tool_calls:
            self._report()
        if event.tokens is None or (event.usage_id is not None and event.usage_id in self.counted):
            return None
        if event.usage_id is not None:
            self.counted.add(event.usage_id)
        self.tokens.add(event.tokens)
        self._report()
        if event.tokens.output > self.output_limit:
            return self._stop("output_tokens", f"单次输出 {event.tokens.output} token，超过上限 {self.output_limit}")
        if self.room is not None and self.tokens.weighted(self.weight) > self.room:
            return self._stop("issue_tokens", "本对象的用量将超过每个 Issue 的上限")
        return None

    def _stop(self, reason: str, text: str) -> str:
        self.reason = text
        return reason

    def _report(self) -> None:
        if self.run.progress is not None:
            self.run.progress(self.run.turns + self.turns, _sum(self.run.tokens, self.tokens))


def _executable(tool: str, context: AgentContext) -> str | None:
    """配置了路径就用配置的(文件不存在即不可用，不悄悄改用 PATH)，否则按 PATH 查找。"""
    try:
        configured = context.settings.get(f"tools.{tool}.path")
    except MissingSetting:
        configured = None
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() else None
    return shutil.which(tool, path=context.environ.get("PATH"))


def _transient(text: str, settings: Settings) -> bool:
    lowered = text.lower()
    return any(str(pattern).lower() in lowered for pattern in settings.get("limits.transientPatterns"))


def _record_dependency(context: AgentContext, tool: str, status: CallStatus) -> None:
    if context.replay is not None or status in NOT_COUNTED:
        return
    if status is CallStatus.AUTH_FAILED:
        context.breaker.trip(tool)
    else:
        context.breaker.record(tool, status not in DEPENDENCY_FAILURES)


def _estimate(tokens: Tokens, model: Model) -> float | None:
    """工具不报费用时按别名中的价格(每百万 token 的美元)估算；缓存读写没有单独价格时按输入价。"""
    if model.price_input is None or model.price_output is None:
        return None
    cache_read = model.price_cache_read if model.price_cache_read is not None else model.price_input
    cache_write = model.price_cache_write if model.price_cache_write is not None else model.price_input
    uncached = tokens.input - tokens.cache_read - tokens.cache_write
    return (uncached * model.price_input + tokens.cache_read * cache_read + tokens.cache_write * cache_write
            + tokens.output * model.price_output) / MILLION


# 结构化结果


def checked_output(parsed: Parsed, schema: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """工具原生的结构化结果优先，否则从最终文本中提取；按 schema 校验，返回结果与每条错误(带 JSON 路径)。"""
    value = parsed.structured if parsed.structured is not None else extract_json(parsed.text)
    if value is None:
        return None, [NO_JSON]
    if not isinstance(value, dict):
        return None, [NOT_OBJECT]
    errors = schema_errors(value, schema)
    return (None, errors) if errors else (value, [])


def extract_json(text: str | None) -> Any:
    """依次尝试：整段文本、最后一个 json 代码块、最后一个括号配平的顶层对象；都不能解析时返回 None。"""
    if not text or not text.strip():
        return None
    whole = _parse(text.strip())
    if whole is not None:
        return whole
    blocks = CODE_BLOCK.findall(text)
    if blocks:
        value = _parse(blocks[-1].strip())
        if value is not None:
            return value
    balanced = _last_balanced_object(text)
    return None if balanced is None else _parse(balanced)


def raw_output(parsed: Parsed) -> str | None:
    """重试说明中的原输出：带 schema 的工具把结果放在 structured，最终文本常为空，不能显示成空。"""
    return parsed.text if parsed.structured is None else json.dumps(parsed.structured, ensure_ascii=False)


def retry_note(raw: str | None, errors: Sequence[str]) -> str:
    lines = ["上一次的输出不符合要求的 schema，请只输出一个符合该 schema 的 JSON 对象。逐条错误："]
    lines += [f"- {error}" for error in errors]
    shown = (raw or "")[:RETRY_OUTPUT_CHARS]
    lines += ["上一次的原输出：", shown if shown else "(空)"]
    return "\n".join(lines)


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _last_balanced_object(text: str) -> str | None:
    """最后一个括号配平的顶层 `{...}`；字符串中的括号与转义字符不计。"""
    found: str | None = None
    depth, start, in_string, escaped = 0, -1, False, False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"' and depth > 0:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                found = text[start:index + 1]
    return found


# 边界快照


@dataclass
class _Before:
    """调用前的边界快照：git 状态(路径、越界种类、快照)与 worktree 之外不可写路径的文件快照。"""

    git: list[tuple[Path, str, TreeSnapshot]]
    roots: list[Path]
    files: dict[str, FileState]


def _credentials(params: CallParams, context: AgentContext) -> list[str]:
    """启动前：工作目录(git 仓库或 worktree)中未跟踪的凭据文件与仓库本地配置中的凭据；不是 git 仓库的不查。"""
    if not (params.workdir / ".git").exists():
        return []
    patterns = tuple(context.settings.get("boundaries.protected.forbidden"))
    return [f"credential：{item}" for item in credentials_present(params.workdir, context.runner, patterns)]


def _snapshots(params: CallParams, context: AgentContext) -> _Before:
    """只读步骤：工作目录不许有任何变化；可写步骤：工作目录之外的主仓库与不可写路径不许变化。不是 git 仓库的不比对。"""
    if params.access is Access.READ:
        targets = [(params.workdir, "read_only_changed")]
        roots: list[Path] = []
    else:
        repo = context.settings.project.repo if context.settings.project is not None else None
        targets = [] if repo is None or repo.resolve() == params.workdir.resolve() else [(repo, "outside")]
        roots = _protected(params, context)
    git = [(path, kind, snapshot(path, context.runner)) for path, kind in targets if (path / ".git").exists()]
    return _Before(git, roots, file_snapshot(roots))


def _compare(before: _Before, context: AgentContext) -> list[str]:
    violations = [f"{kind}：{path} {change}" for path, kind, snap in before.git
                  for change in compare(snap, snapshot(path, context.runner), path, context.runner)]
    if before.roots:
        after = file_snapshot(before.roots, before.files)
        violations += [f"outside：agent 不可写的 {path} 被修改" for path in changed_files(before.files, after)]
    return violations


def _protected(params: CallParams, context: AgentContext) -> list[Path]:
    """worktree 之外 agent 不可写的路径：工作区配置(接入清单、settings、sites、secrets、脚本)与 tightrein 自身的代码、
    全局配置、外部 skills。与工作目录互相包含的不算(在工作目录里写是本步骤的工作)。"""
    layout, tool = context.layout, context.tool
    candidates = (layout.setup, layout.setup_md, layout.settings, layout.sites, layout.secrets, layout.scripts_dir,
                  tool.root / "src", tool.settings_dir, tool.vendor_dir)
    workdir = params.workdir.resolve()
    return [path for path in candidates
            if not (workdir.is_relative_to(path.resolve()) or path.resolve().is_relative_to(workdir))]


def _hidden(params: CallParams, context: AgentContext) -> list[Path]:
    """agent 不得读取的位置：工作区的 data/ 与家目录下的凭据位置；工作目录与额外可读目录在检查时放行。"""
    home = context.environ.get("HOME")
    return [context.layout.data_dir, *((Path(home) / name for name in HIDDEN_HOME) if home else ())]


# 文件与记录


def _finish(params: CallParams, context: AgentContext, result: CallResult, marker: Path, started: str,
            run: _Run) -> CallResult:
    if not result.ok or context.debug:
        result.raw_path = _save(params, context, run)
    _mark(marker, params, run.model, started, result, context.clock, fallback_from=run.fallback_from)
    summary = f"{result.tool}/{result.model}：{result.status.value}"
    if result.error:
        summary += f"，{result.error[:200]}"
    refs = {"started": str(marker)} | ({"raw": str(result.raw_path)} if result.raw_path is not None else {})
    _emit(context, params, "action", summary, refs)
    return result


def _save(params: CallParams, context: AgentContext, run: _Run) -> Path | None:
    """失败或调试时保存提示与原始输出：写入前脱敏，单个过长的字符串截断，不认识的行原样保留。"""
    write_text(_file(params, context.layout, "prompt", "md"), context.redactor.text(params.prompt))
    if not run.raw:
        return None
    path = _file(params, context.layout, "raw", "jsonl")
    write_text(path, "\n".join(_raw_line(line, context.redactor) for line in run.raw))
    return path


def _raw_line(line: str, redactor: Redactor) -> str:
    value = _parse(line)
    if not isinstance(value, (dict, list)):
        return truncate(redactor.text(line), TOOL_OUTPUT_CHARS)
    return json.dumps(_truncated(redactor.mapping(value)), ensure_ascii=False)


def _truncated(value: Any) -> Any:
    if isinstance(value, str):
        return truncate(value, TOOL_OUTPUT_CHARS)
    if isinstance(value, dict):
        return {key: _truncated(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncated(item) for item in value]
    return value


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATED):  # 上限比截断标记还短时只截断，不加标记，结果不超过上限
        return text[:limit]
    return text[:limit - len(TRUNCATED)] + TRUNCATED


def _separator(number: int, model: Model, outcome: Outcome) -> str:
    """raw 中每次工具调用前的一行，回放录制时去掉。"""
    return json.dumps({"tightrein": {"attempt": number, "tool": model.tool, "model": model.model,
                                     "exitCode": outcome.exit_code, "stoppedBy": outcome.stopped_by,
                                     "stderr": outcome.stderr_tail}}, ensure_ascii=False)


def _mark(path: Path, params: CallParams, model: Model, started: str, result: CallResult | None,
          clock: Clock, *, turns: int | None = None, tokens: Tokens | None = None,
          fallback_from: Mapping[str, str] | None = None) -> None:
    """started 标记：endedAt 为空即表示调用正在进行，模型重试或等待时 watch 不会显示成卡死。"""
    if result is not None:
        turns, tokens = result.turns, result.tokens
    write_json(path, {
        "point": params.point, "subject": params.subject, "run": params.run, "tool": model.tool,
        "model": model.model, "effort": model.effort, "startedAt": started,
        "endedAt": None if result is None else format_iso(clock.now()),
        "status": None if result is None else result.status.value,
        "durationMs": None if result is None else result.duration_ms,
        "turns": turns,
        "tokens": None if tokens is None else {"input": tokens.input, "output": tokens.output,
                                                "cacheRead": tokens.cache_read, "cacheWrite": tokens.cache_write},
        "fallbackFrom": None if fallback_from is None else dict(fallback_from),
    })


def _progress(path: Path, params: CallParams, model: Model, started: str,
              clock: Clock) -> Callable[[int, Tokens], None]:
    """进行中的标记最多每 PROGRESS_S 秒写一次，逐行写盘会拖慢读输出。"""
    last = [0.0]

    def report(turns: int, tokens: Tokens) -> None:
        now = time.monotonic()
        if now - last[0] >= PROGRESS_S:
            last[0] = now
            _mark(path, params, model, started, None, clock, turns=turns, tokens=tokens)

    return report


def _sum(left: Tokens, right: Tokens) -> Tokens:
    total = Tokens()
    total.add(left)
    total.add(right)
    return total


def _file(params: CallParams, layout: WorkspaceLayout, content: str, extension: str) -> Path:
    name = FileName(params.point, content, extension, round=params.round, sequence=_sequence(params.point))
    return layout.step_file(params.subject or params.run, name)


def _sequence(point: str) -> int:
    """没有登记文件序号的调用点按阶段取整十(如 retro.idea → 50)，仍按阶段排序；不属于阶段的(knowledge 等)为 00。"""
    try:
        return step_sequence(point)
    except ValueError:
        stage = point.split(".")[0]
        return (STAGES.index(stage) + 1) * 10 if stage in STAGES else 0


def _emit(context: AgentContext, params: CallParams, kind: str, summary: str, refs: Mapping[str, str] = {}) -> None:
    context.events.emit(run=params.run, subject=params.subject, point=params.point, kind=kind, summary=summary,
                        refs=refs)
