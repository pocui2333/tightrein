"""执行器的完整流程(architecture/02 2.8、2.10、2.11)。

run：
1. 按 runner/runner-task.schema.json 校验任务，解析工具与模型，limits 中为空的项取 stages.<环节>.limits；
2. 当天的费用已达 stages.<环节>.budgetPerDay，或全部环节合计已达 budget 段的每天或每周上限时不启动，返回
   limit-reached、daily-budget；
3. 在 invoke_agent span 中：guards.before；写提示与 schema 文件；启动进程，逐行写原始输出与统一事件，同时计数与累计费用，
   到达上限时终止；无论怎样结束都执行 guards.after；
4. 有违规时为 guard-violation，不做格式重试；否则取结构化结果按 outputSchema 校验，第一次不通过时生成重试说明，
   工具支持按会话续接的续接同一会话，否则新开一次调用并在提示末尾附上说明；第二次仍不通过为 schema-invalid；
5. 把全部尝试的费用累加进 budget_usage，写 result.json 与 task.json，结束 span。
--output 模式下 agent 进程的环境带 TIGHTREIN_SANDBOX=1 与输出目录 TIGHTREIN_OUTPUT_DIR，agent 调用的 kb 命令与 MCP 服务
据此不记命中、不同步，事件写到输出目录。
run_interactive 与 resume_interactive 启动或续接交互会话：不设轮数与单次费用上限，不因超时终止，不做只读锁定；
结束后读取工具本机保存的会话记录写入统一格式，只检查 git 状态与隐藏路径的读取；outputSchema 不为空时以无人值守方式
续接同一会话做一次收尾调用取结构化结果。
所有失败都以 RunnerResult 返回；只有任务不合 schema、配置缺项、工具名未知这类编程错误抛出 RunnerConfigError。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.routes import ModelChoice
from tightrein.config.project import ProjectConfig
from tightrein.contracts import validate
from tightrein.contracts.validate import SchemaValidationError
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import RunnerStatus
from tightrein.guards.report import GuardBlocked, Violation, blocking
from tightrein.guards.service import GuardContext, Guards
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Span, Tracer
from tightrein.runner import limits, output, registry, sessions
from tightrein.runner.adapters.base import (
    ENDED_BUDGET_LIMIT,
    ENDED_ERROR,
    ENDED_TURN_LIMIT,
    Adapter,
    InvocationFiles,
    ParsedRun,
    RetryContext,
    SessionRef,
    to_events,
)
from tightrein.runner.adapters.replay import NAME as REPLAY, ReplayAdapter
from tightrein.runner.process import TIMEOUT, ProcessLauncher
from tightrein.runner.prompt import build_prompt
from tightrein.runner.recording import task_sha256
from tightrein.runner.registry import Registry
from tightrein.runner.result import (
    COST_LIMIT,
    DAILY_BUDGET,
    RESUME_UNSUPPORTED,
    TOOL_ERROR,
    TOOL_UNAVAILABLE,
    TURN_LIMIT,
    RunnerConfigError,
    RunnerResult,
    Usage,
)
from tightrein.runner.task import RunnerTask
from tightrein.runner.transcript import ERROR, SYSTEM, EventDraft, TranscriptWriter
from tightrein.store.files import atomic
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

ENV_WORKSPACE = "TIGHTREIN_WORKSPACE"
ENV_SANDBOX = "TIGHTREIN_SANDBOX"
ENV_OUTPUT_DIR = "TIGHTREIN_OUTPUT_DIR"
MILLISECONDS_PER_SECOND = 1000
FINALIZE_STDOUT = "stdout.finalize.jsonl"
ENDINGS = {ENDED_TURN_LIMIT: TURN_LIMIT, ENDED_BUDGET_LIMIT: COST_LIMIT}


def raw_output(parsed: ParsedRun) -> str | None:
    """重试说明中的原输出：带 --json-schema 的工具把结果放在 structured，final_text 常为空。"""
    return parsed.final_text if parsed.structured is None else json.dumps(parsed.structured, ensure_ascii=False)


@dataclass
class Call:
    """一次调用的结果：status 不为空时本次尝试已经确定了结果，不再校验输出。"""

    parsed: ParsedRun
    usage: Usage
    status: RunnerStatus | None = None
    error_type: str | None = None
    tool: str | None = None
    model: str | None = None


@dataclass
class Attempts:
    tool: str
    model: str | None
    effort: str | None = None
    usages: list[Usage] = field(default_factory=list)
    count: int = 0
    session_id: str | None = None
    detail: str | None = None

    @property
    def usage(self) -> Usage:
        return Usage.total(self.usages)


class Runner:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        layout: WorkspaceLayout,
        tool_layout: ToolLayout,
        config: ProjectConfig,
        registry: Registry,
        guards: Guards,
        launcher: ProcessLauncher,
        tracer: Tracer,
        redactor: Redactor,
        environ: Mapping[str, str],
        zone: tzinfo | None = None,
        replay: ReplayAdapter | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conn = conn
        self.layout = layout
        self.tool_layout = tool_layout
        self.config = config
        self.registry = registry
        self.guards = guards
        self.launcher = launcher
        self.tracer = tracer
        self.redactor = redactor
        self.environ = dict(environ)
        self.zone = zone
        self.replay = replay
        self.monotonic = monotonic

    # 共用

    def _validated(self, task: RunnerTask) -> RunnerTask:
        try:
            task.to_dict()
        except SchemaValidationError as error:
            raise RunnerConfigError(f"任务不合 runner/runner-task.schema.json：{error}") from error
        return task.with_limits(task.limits.filled(limits.stage_limits(self.config, task.stage)))

    def _relative(self, task: RunnerTask, path: Path) -> str:
        return path.relative_to(self.layout.run_dir(task.run_id)).as_posix()

    def _transcript(self, task: RunnerTask, clock: Clock) -> TranscriptWriter:
        return TranscriptWriter(self.layout.transcript(task.run_id, task.role, task.subject_id), self.redactor, clock,
                                run_id=task.run_id, role=task.role, subject_id=task.subject_id,
                                tool_output_chars=int(self.config.get("runtime.runner.toolOutputMaxChars")))

    def _agent_env(self, context: GuardContext) -> dict[str, str]:
        env = {**context.env, **self.tracer.child_environment(), ENV_WORKSPACE: str(self.layout.root)}
        if self.layout.output_dir is not None:
            env[ENV_SANDBOX] = "1"
            env[ENV_OUTPUT_DIR] = str(self.layout.output_dir)
        return env

    def _schema(self, task: RunnerTask) -> dict[str, Any] | None:
        return None if task.output_schema is None else validate.inline(task.output_schema)

    def _write_files(self, task: RunnerTask, files: InvocationFiles, schema: dict[str, Any] | None,
                     prompt_schema: dict[str, Any] | None, note: str | None) -> None:
        files.directory.mkdir(parents=True, exist_ok=True)
        files.prompt.write_text(build_prompt(task, self.tool_layout, prompt_schema, note,
                                                     self.config.language), encoding="utf-8")
        if schema is not None:
            files.schema.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")

    def _result(self, task: RunnerTask, status: RunnerStatus, attempts: Attempts, started: float, *,
                error_type: str | None = None, output_value: Mapping[str, Any] | None = None,
                violations: tuple[Violation, ...] = (), transcript: bool = True,
                report: Path | None = None) -> RunnerResult:
        transcript_path = self.layout.transcript(task.run_id, task.role, task.subject_id)
        return RunnerResult(
            status=status, tool=attempts.tool, model=attempts.model, error_type=error_type,
            output=output_value if status is RunnerStatus.OK else None, usage=attempts.usage,
            duration_ms=round((self.monotonic() - started) * MILLISECONDS_PER_SECOND), attempts=attempts.count,
            session_id=attempts.session_id,
            transcript_path=self._relative(task, transcript_path) if transcript and transcript_path.exists() else None,
            guard_report=None if report is None or not report.exists() else self._relative(task, report),
            violations=violations,
        )

    def _save(self, task: RunnerTask, result: RunnerResult) -> None:
        files = InvocationFiles(self.layout.runner_raw_dir(task.run_id, task.role, task.subject_id))
        atomic.write_text(files.result, json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
        task_data = {**task.to_dict(), "taskSha256": task_sha256(task)}
        atomic.write_text(files.directory / "task.json", json.dumps(task_data, ensure_ascii=False, indent=2) + "\n")

    def _mark_started(self, task: RunnerTask, attempts: Attempts, clock: Clock) -> None:
        files = InvocationFiles(self.layout.runner_raw_dir(task.run_id, task.role, task.subject_id))
        files.directory.mkdir(parents=True, exist_ok=True)
        marker = {"role": task.role, "subject": task.subject_id, "tool": attempts.tool, "model": attempts.model,
                  "effort": attempts.effort, "startedAt": format_iso(clock.now())}
        atomic.write_text(files.started, json.dumps(marker, ensure_ascii=False) + "\n")

    def _finish_span(self, span: Span, result: RunnerResult, detail: str | None) -> None:
        span.set(agent=result.tool, model=result.model, input_tokens=result.usage.input_tokens,
                 output_tokens=result.usage.output_tokens, cost_usd=result.usage.cost_usd,
                 status=result.status.value, error_type=result.error_type, reason=detail,
                 attributes={"attempts": result.attempts, "sessionId": result.session_id})

    def _daily_budget_reached(self, task: RunnerTask, clock: Clock) -> bool:
        """该环节当天的上限，或全部环节合计的每天、每周上限(budget 段)已到达。"""
        budget = limits.DailyBudget(self.conn, clock, self.zone)
        total = limits.GlobalBudget(self.conn, clock, limits.BudgetLimits.from_config(self.config), self.zone)
        return budget.exhausted(task.stage, limits.budget_per_day(self.config, task.stage)) or \
            total.exceeded() is not None

    def _record(self, task: RunnerTask, clock: Clock, result: RunnerResult) -> RunnerResult:
        limits.DailyBudget(self.conn, clock, self.zone).add(task.stage, result.usage)
        self._save(task, result)
        return result

    def _blocked(self, task: RunnerTask, attempts: Attempts, started: float,
                 violations: tuple[Violation, ...]) -> RunnerResult:
        first = (blocking(violations) or violations)[0]
        return self._result(task, RunnerStatus.GUARD_VIOLATION, attempts, started, error_type=first.kind.value,
                            violations=violations,
                            report=self.layout.guard_report(task.run_id, task.role, task.subject_id))

    # 无人值守

    def run(self, task: RunnerTask, *, clock: Clock, runner_override: str | None = None,
            model_override: str | None = None, resume_session: str | None = None) -> RunnerResult:
        """resume_session 给出时第一次调用就续接该会话(同一会话的下一轮，任务说明作为这一轮的输入)；
        工具不支持按会话续接或回放时照常新开会话。"""
        task = self._validated(task)
        if task.interactive:
            raise RunnerConfigError("交互任务须经 run_interactive 执行")
        started = self.monotonic()
        replaying = runner_override == REPLAY  # --runner replay 时各调用点都回放
        if replaying:
            if self.replay is None:
                raise RunnerConfigError("没有提供回放录制集(--replay-from)")
            attempts = Attempts(REPLAY, None)
            adapter: Adapter | None = None
        else:
            choice = registry.choose(self.config, task, runner_override=runner_override, model_override=model_override)
            attempts = Attempts(choice.tool, choice.model, choice.effort)
            adapter = self.registry.adapter(choice.tool)
        if self._daily_budget_reached(task, clock):
            result = self._result(task, RunnerStatus.LIMIT_REACHED, attempts, started, error_type=DAILY_BUDGET,
                                  transcript=False)
            self._save(task, result)
            return result
        attributes = {"role": task.role, "subjectId": task.subject_id, "effort": attempts.effort}
        self._mark_started(task, attempts, clock)
        with self.tracer.span("invoke_agent", agent=attempts.tool, model=attempts.model,
                              attributes=attributes) as span:
            result = self._run_attempts(task, clock, adapter, attempts, started, resume_session)
            self._finish_span(span, result, attempts.detail)
        return self._record(task, clock, result)

    def _run_attempts(self, task: RunnerTask, clock: Clock, adapter: Adapter | None, attempts: Attempts,
                      started: float, resume_session: str | None = None) -> RunnerResult:
        transcript = self._transcript(task, clock)
        schema = self._schema(task)
        retry: RetryContext | None = None
        if resume_session is not None and adapter is not None and adapter.supports_resume_by_id:
            retry = RetryContext(task.instructions.prompt, resume_session)
            attempts.session_id = resume_session
        for number in range(1, int(self.config.get("runtime.runner.maxCalls")) + 1):
            files = InvocationFiles(self.layout.runner_raw_dir(task.run_id, task.role, task.subject_id), number)
            extra_env = adapter.env_names if adapter is not None else ()
            try:
                context = self.guards.before(task, clock=clock, base_env=self.environ, extra_env_names=extra_env)
            except GuardBlocked as blocked:
                return self._blocked(task, attempts, started, blocked.violations)
            attempts.count += 1
            transcript.begin_call()
            try:
                if adapter is None:
                    call = self._replay_call(task, number, transcript)
                else:
                    resumable = retry is not None and adapter.supports_resume_by_id and retry.session_id is not None
                    note = None if retry is None or resumable else retry.note
                    self._write_files(task, files, schema, None if adapter.supports_schema else schema, note)
                    call = self._process_call(task, adapter, files, self._agent_env(context), retry, attempts,
                                              transcript)
            finally:
                report = self.guards.after(task, context, transcript.tool_calls())
            attempts.usages.append(call.usage)
            attempts.session_id = call.parsed.session_id or attempts.session_id
            if call.tool is not None:
                attempts.tool, attempts.model = call.tool, call.model
            if not report.ok:
                return self._blocked(task, attempts, started, report.violations)
            if call.status is not None:
                return self._result(task, call.status, attempts, started, error_type=call.error_type,
                                    report=context.report_path)
            checked = output.check_output(task.output_schema or "", call.parsed.structured, call.parsed.final_text)
            if checked.ok:
                return self._result(task, RunnerStatus.OK, attempts, started, output_value=checked.value,
                                    report=context.report_path)
            parsed = call.parsed
            note = output.retry_note(task.output_schema or "", raw_output(parsed), checked.errors,
                                     int(self.config.get("runtime.runner.retryOutputChars")))
            transcript.write(EventDraft(ERROR, SYSTEM, text=note), tool=attempts.tool, model=attempts.model,
                             session_id=attempts.session_id)
            retry = RetryContext(note, attempts.session_id)
        return self._result(task, RunnerStatus.SCHEMA_INVALID, attempts, started,
                            report=self.layout.guard_report(task.run_id, task.role, task.subject_id))

    def _replay_call(self, task: RunnerTask, number: int, transcript: TranscriptWriter) -> Call:
        if self.replay is None:
            raise RunnerConfigError("没有提供回放录制集(--replay-from)")
        replayed = self.replay.call(task, number)
        transcript.write_all(replayed.events, tool=replayed.tool, model=replayed.model,
                             session_id=replayed.parsed.session_id)
        status, error_type = replayed.forced if replayed.forced is not None else (None, None)
        return Call(replayed.parsed, replayed.parsed.usage, status, error_type, replayed.tool, replayed.model)

    def _process_call(self, task: RunnerTask, adapter: Adapter, files: InvocationFiles, env: dict[str, str],
                      retry: RetryContext | None, attempts: Attempts, transcript: TranscriptWriter) -> Call:
        executable = self.registry.executable(adapter.name)
        missing = ParsedRun(None, None, None, Usage(), None, ENDED_ERROR, f"找不到 {adapter.name} 的可执行文件")
        if executable is None:
            return self._unavailable(adapter, attempts, transcript, missing)
        invocation = adapter.build(task, files, executable=executable, model=attempts.model, env=env, retry=retry,
                                   effort=attempts.effort)
        cost = self._costing(attempts)
        watch = limits.RunWatch(
            max_turns=None if adapter.supports_turn_limit else task.limits.max_turns,
            max_cost_usd=None if adapter.supports_budget_limit else task.limits.max_cost_usd, cost=cost,
        )
        session_id = retry.session_id if retry is not None else None
        with open(files.stdout, "w", encoding="utf-8") as raw:
            def on_line(line: str) -> str | None:
                nonlocal session_id
                raw.write(line + "\n")
                raw.flush()
                stop = None
                for draft in adapter.convert(line):
                    session_id = draft.session_id or session_id
                    transcript.write(draft, tool=adapter.name, model=attempts.model, session_id=session_id)
                    stop = stop or watch.observe(draft)
                return stop

            try:
                outcome = self.launcher.run(invocation, on_line, task.limits.max_duration_ms)
            except OSError:
                return self._unavailable(adapter, attempts, transcript, missing)
        parsed = adapter.parse(files.stdout, outcome.exit_code)
        transcript.write_all(adapter.closing_events(parsed), tool=adapter.name, model=attempts.model,
                             session_id=parsed.session_id)
        usage = cost(parsed.usage)
        if outcome.stopped_by is not None:
            error_type = TIMEOUT if outcome.stopped_by == TIMEOUT else outcome.stopped_by
            return Call(parsed, usage, RunnerStatus.LIMIT_REACHED, error_type)
        if parsed.ended_by in ENDINGS:
            return Call(parsed, usage, RunnerStatus.LIMIT_REACHED, ENDINGS[parsed.ended_by])
        if parsed.ended_by == ENDED_ERROR or outcome.exit_code != 0:
            detail = "\n".join(part for part in (parsed.error_message, outcome.stderr_tail) if part)
            attempts.detail = f"退出码 {outcome.exit_code}：{detail}"
            transcript.write(EventDraft(ERROR, SYSTEM, text=attempts.detail), tool=adapter.name, model=attempts.model,
                             session_id=parsed.session_id)
            return Call(parsed, usage, RunnerStatus.FAILED, TOOL_ERROR)
        return Call(parsed, usage)

    def _costing(self, attempts: Attempts) -> Callable[[Usage], Usage]:
        def cost(usage: Usage) -> Usage:
            return limits.with_cost(usage, self.config.routes, attempts.tool, attempts.model)
        return cost

    def _unavailable(self, adapter: Adapter, attempts: Attempts, transcript: TranscriptWriter,
                     parsed: ParsedRun) -> Call:
        text = f"找不到 {adapter.name} 的可执行文件或无法启动：在 ~/.config/tightrein/config.yaml 的 " \
               f"tools.{adapter.name}.path 中配置路径，或安装后确认已登录"
        transcript.write(EventDraft(ERROR, SYSTEM, text=text), tool=adapter.name, model=attempts.model,
                         session_id=None)
        return Call(parsed, Usage(), RunnerStatus.FAILED, TOOL_UNAVAILABLE)

    # 交互

    def run_interactive(self, task: RunnerTask, *, clock: Clock, first_input: str) -> RunnerResult:
        task = self._validated(task)
        if not task.interactive:
            raise RunnerConfigError("run_interactive 只接受 interactive 为真的任务")
        choice = registry.choose(self.config, task)
        adapter = self.registry.adapter(choice.tool)
        new_id = adapter.new_session_id()
        return self._interactive(task, clock, first_input, adapter, choice,
                                 None if new_id is None else SessionRef(new_id), resume=False)

    def resume_interactive(self, task: RunnerTask, *, clock: Clock, first_input: str) -> RunnerResult:
        """续接该对象、该角色最近一次交互会话；找不到会话或工具无法续接时返回 resume-unsupported，由调用方新开会话。"""
        task = self._validated(task)
        started = self.monotonic()
        latest = sessions.latest(self.conn, task)
        tool = latest.tool if latest is not None else registry.choose(self.config, task).tool
        attempts = Attempts(tool, None)
        if latest is None or latest.session_id is None:
            return self._record(task, clock, self._result(task, RunnerStatus.FAILED, attempts, started,
                                                          error_type=RESUME_UNSUPPORTED, transcript=False))
        adapter = self.registry.adapter(latest.tool)
        if not adapter.supports_resume_by_id or self.registry.executable(adapter.name) is None:
            return self._record(task, clock, self._result(task, RunnerStatus.FAILED, attempts, started,
                                                          error_type=RESUME_UNSUPPORTED, transcript=False))
        choice = registry.choose(self.config, task, runner_override=latest.tool)
        return self._interactive(task, clock, first_input, adapter, choice,
                                 SessionRef(latest.session_id), resume=True)

    def _interactive(self, task: RunnerTask, clock: Clock, first_input: str, adapter: Adapter, choice: ModelChoice,
                     session: SessionRef | None, *, resume: bool) -> RunnerResult:
        started = self.monotonic()
        attempts = Attempts(adapter.name, choice.model, choice.effort)
        if self._daily_budget_reached(task, clock):
            return self._record(task, clock, self._result(task, RunnerStatus.LIMIT_REACHED, attempts, started,
                                                          error_type=DAILY_BUDGET, transcript=False))
        attributes = {"role": task.role, "subjectId": task.subject_id, "interactive": True, "effort": attempts.effort}
        with self.tracer.span("invoke_agent", agent=adapter.name, model=attempts.model, attributes=attributes) as span:
            result = self._interactive_session(task, clock, first_input, adapter, attempts, session, resume, started)
            self._finish_span(span, result, attempts.detail)
        return self._record(task, clock, result)

    def _interactive_session(self, task: RunnerTask, clock: Clock, first_input: str, adapter: Adapter,
                             attempts: Attempts, session: SessionRef | None, resume: bool,
                             started: float) -> RunnerResult:
        executable = self.registry.executable(adapter.name)
        if executable is None:
            return self._result(task, RunnerStatus.FAILED, attempts, started, error_type=TOOL_UNAVAILABLE,
                                transcript=False)
        try:
            context = self.guards.before(task, clock=clock, base_env=self.environ, extra_env_names=adapter.env_names)
        except GuardBlocked as blocked:
            return self._blocked(task, attempts, started, blocked.violations)
        files = InvocationFiles(self.layout.runner_raw_dir(task.run_id, task.role, task.subject_id))
        self._write_files(task, files, self._schema(task), None, None)
        started_at = clock.now()
        record = sessions.open_session(self.conn, task, adapter.name, started_at,
                                       None if session is None else session.session_id)
        attempts.count = 1
        invocation = adapter.build_interactive(task, files, first_input, executable=executable, model=attempts.model,
                                               env=self._agent_env(context), session=session, resume=resume,
                                               effort=attempts.effort)
        transcript = self._transcript(task, clock)
        transcript.begin_call()
        try:
            self.launcher.run_interactive(invocation)
        except OSError:
            self.guards.after(task, context)
            sessions.close(self.conn, record, clock.now())
            return self._result(task, RunnerStatus.FAILED, attempts, started, error_type=TOOL_UNAVAILABLE)
        located = adapter.locate_session(task.workdir, started_at, None if session is None else session.session_id)
        if located is None and session is not None:
            located = session
        drafts = adapter.session_events(located) if located is not None else []
        attempts.session_id = located.session_id if located is not None else None
        transcript.write_all(drafts, tool=adapter.name, model=attempts.model, session_id=attempts.session_id)
        report = self.guards.after(task, context, transcript.tool_calls())
        cost = self._costing(attempts)
        attempts.usages = [cost(draft.usage) for draft in drafts if draft.usage is not None]
        record = sessions.attach(self.conn, record, attempts.session_id)
        try:
            if not report.ok:
                return self._blocked(task, attempts, started, report.violations)
            if task.output_schema is None:
                return self._result(task, RunnerStatus.OK, attempts, started, output_value={},
                                    report=context.report_path)
            return self._finalize(task, adapter, files, located, attempts, transcript, context, started)
        finally:
            sessions.close(self.conn, record, clock.now())

    def _finalize(self, task: RunnerTask, adapter: Adapter, files: InvocationFiles, located: SessionRef | None,
                  attempts: Attempts, transcript: TranscriptWriter, context: GuardContext,
                  started: float) -> RunnerResult:
        """以无人值守方式续接同一会话，要求按 schema 给出结构化结果；不做格式重试。"""
        executable = self.registry.executable(adapter.name)
        if located is None or not adapter.supports_resume_by_id or executable is None:
            return self._result(task, RunnerStatus.FAILED, attempts, started, error_type=RESUME_UNSUPPORTED,
                                report=context.report_path)
        invocation = adapter.build_finalize(task, files, located, executable=executable, model=attempts.model,
                                            env=self._agent_env(context), effort=attempts.effort)
        stdout = files.directory / FINALIZE_STDOUT
        with open(stdout, "w", encoding="utf-8") as raw:
            def keep(line: str) -> None:
                raw.write(line + "\n")

            try:
                outcome = self.launcher.run(invocation, keep, None)
            except OSError:
                return self._result(task, RunnerStatus.FAILED, attempts, started, error_type=TOOL_UNAVAILABLE,
                                    report=context.report_path)
        attempts.count += 1
        transcript.begin_call()
        transcript.write_all(to_events(adapter, stdout), tool=adapter.name, model=attempts.model,
                             session_id=located.session_id)
        parsed = adapter.parse(stdout, outcome.exit_code)
        attempts.usages.append(self._costing(attempts)(parsed.usage))
        if parsed.ended_by == ENDED_ERROR or outcome.exit_code != 0:
            return self._result(task, RunnerStatus.FAILED, attempts, started, error_type=TOOL_ERROR,
                                report=context.report_path)
        checked = output.check_output(task.output_schema or "", parsed.structured, parsed.final_text)
        if not checked.ok:
            note = output.retry_note(task.output_schema or "", raw_output(parsed), checked.errors,
                                     int(self.config.get("runtime.runner.retryOutputChars")))
            transcript.write(EventDraft(ERROR, SYSTEM, text=note), tool=adapter.name, model=attempts.model,
                             session_id=located.session_id)
            return self._result(task, RunnerStatus.SCHEMA_INVALID, attempts, started, report=context.report_path)
        return self._result(task, RunnerStatus.OK, attempts, started, output_value=checked.value,
                            report=context.report_path)
