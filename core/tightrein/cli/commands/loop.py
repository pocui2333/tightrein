"""全局命令：run、tick、status、next、continue、find、pending、confirm、reject、pause、resume(architecture/09 4.1、4.4，
redesign/09-loop.md 第 5、6 节)。pause 与 resume 不带 --workspace 时作用于全局(本机状态目录中的标记文件)。"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

from tightrein.cli import confirm as confirming
from tightrein.cli import exit_codes, selectors
from tightrein.cli.commands.common import leaf, orchestrator
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome, error
from tightrein.domain.enums import OperationStatus, ProbeLevel, RunStage, RunStatus, Stage
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.clock import SystemClock
from tightrein.orchestrator import pause, resume
from tightrein.orchestrator.rules import RunRequest
from tightrein.store.files.layout import UserLayout
from tightrein.store.locks import FileLockBusy
from tightrein.store.repos import pending_operations, runs
from tightrein.vcs.executor import OperationResult
from tightrein.vcs.operations import PendingOperation

FROM_CHOICES = [stage.value for stage in resume.FROM_STAGES]
UNTIL_CHOICES = [stage.value for stage in resume.ORDER]


def _run(invocation: Any) -> Outcome:
    args = invocation.args
    if len(args.select) > 1:
        raise UsageError("run 的 --select 只能给一个链，例如 triage+ 或 +aggregate")
    chain = selectors.modules_of(selectors.parse_chain(args.select[0])) if args.select else None
    probe = ProbeKind(args.probe) if args.probe else None
    if chain is not None and Stage.COLLECT in chain and probe is None:
        raise UsageError("链中包含 collect 时需要 --probe")
    request = RunRequest(args.scheduled, chain, args.subject, probe, ProbeLevel(args.level) if args.level else None)
    app = invocation.app
    if args.dry_run:
        steps = orchestrator(app).preview(request)
        lines = ["将要执行(按当前状态判断，前面的步骤新产生的问题与 Issue 不计入)：",
                 *(f"- {'执行' if step.executed else '跳过'} {step.name}：{step.reason}" for step in steps)]
        return Outcome("run", exit_codes.OK, lines, result={"dryRun": True, "steps": [step.to_dict() for step in steps]})
    try:
        report = orchestrator(app).run(request)
    except pause.Paused as paused:
        return Outcome("run", exit_codes.PRECONDITION, [f"没有运行：{paused}"], result={"paused": str(paused)})
    except FileLockBusy:
        running = [run.id for run in runs.find(app.conn, stage=RunStage.LOOP, status=RunStatus.RUNNING)]
        message = f"另一次运行正在进行：{'、'.join(running) or '运行锁被占用'}"
        return Outcome("run", exit_codes.LOCKED, [message], errors=[error("FileLockBusy", message)])
    values = report.outputs
    failed = report.run.status is RunStatus.FAILED
    lines = [values["conclusion"], *(f"- [{item['kind']}] {item['subjectId']} {item['summary']}：{item['command']}"
                                     for item in values["waiting"])]
    lines.append(f"每日汇总：{app.layout.relative(report.report)}")
    result = {**values, "runId": report.run.id, "status": report.run.status.value,
              "report": app.layout.relative(report.report)}
    return Outcome("run", exit_codes.FAILED if failed else exit_codes.OK, lines, {"type": "run", "id": report.run.id},
                   result)


def _tick(invocation: Any) -> Outcome:
    app = invocation.app
    try:
        result = orchestrator(app).tick()
    except FileLockBusy:
        return Outcome("tick", exit_codes.OK, ["上一次运行尚未结束，这次跳过"], result={"kind": "busy"})
    values: dict[str, Any] = {"kind": result.kind, "message": result.message}
    lines = [{"paused": "已暂停，没有运行", "idle": "没有运行", "full": "完整运行", "events": "事件运行"}[result.kind]
             + f"：{result.message}"]
    if result.report is not None:
        values.update(runId=result.report.run.id, report=app.layout.relative(result.report.report))
    failed = result.report is not None and result.report.run.status is RunStatus.FAILED
    return Outcome("tick", exit_codes.FAILED if failed else exit_codes.OK, lines, result=values)


def _pause_target(invocation: Any) -> tuple[Path, bool]:
    """全局暂停的标记文件，以及是否作用于单个工作区(给出了 --workspace)。"""
    return UserLayout(invocation.externals.home).pause_flag(), invocation.args.workspace is not None


def _pause(invocation: Any) -> Outcome:
    flag, local = _pause_target(invocation)
    if local:
        app = invocation.app
        pause.pause_workspace(app.conn, app.clock, invocation.args.note)
        return Outcome("pause", exit_codes.OK, [f"工作区 {app.config.name} 已暂停：不再发起新的运行，进行中的步骤完成后停下"],
                       result={"scope": "workspace", "workspace": app.config.name})
    pause.pause_global(flag, SystemClock(), invocation.args.note)
    return Outcome("pause", exit_codes.OK, ["已全局暂停：各工作区不再发起新的运行，进行中的步骤完成后停下"],
                   result={"scope": "global", "flag": str(flag)})


def _resume(invocation: Any) -> Outcome:
    flag, local = _pause_target(invocation)
    if local:
        app = invocation.app
        was = pause.resume_workspace(app.conn)
        return Outcome("resume", exit_codes.OK, [f"工作区 {app.config.name} " + ("已恢复" if was else "没有暂停")],
                       result={"scope": "workspace", "resumed": was})
    was = pause.resume_global(flag)
    return Outcome("resume", exit_codes.OK, ["已恢复全局运行" if was else "没有全局暂停"],
                   result={"scope": "global", "resumed": was})


def _status(invocation: Any) -> Outcome:
    values = orchestrator(invocation.app).status()
    lines = [line for line in (values["paused"], values["onboarding"]) if line]
    lines.append(f"待处理 {len(values['waiting'])} 项")
    lines += [f"- [{item['kind']}] {item['subjectId']} {item['summary']}：{item['command']}"
              f"(推荐：{item['recommendation']})" for item in values["waiting"]]
    last = values["lastRun"]
    lines.append("还没有运行记录" if last is None else f"最近一次运行 {last['runId']}：{last['status']}，"
                                                         f"每日汇总 {last['report']}")
    return Outcome("status", exit_codes.OK, lines, result=values)


def _next(invocation: Any) -> Outcome:
    views = orchestrator(invocation.app).next(invocation.args.subjects)
    lines = []
    for view in views:
        gate = view.to_dict()["gate"]
        can = "能自动继续" if view.step.can_continue else f"不能自动继续({gate or view.step.reason or '无后续步骤'})"
        lines.append(f"{view.ref.id} 当前「{view.status_label}」，下一步：{view.command or '无'}，{can}")
    single = views[0] if len(views) == 1 else None
    return Outcome("next", exit_codes.OK, lines, single.ref.to_dict() if single else None,
                   [view.to_dict() for view in views], next=single.command if single else None)


def _continue_code(report: resume.ContinueReport) -> int:
    if report.failed:
        return exit_codes.FAILED
    if report.gates:
        return exit_codes.GATE
    if any(stop.reason not in (resume.NO_NEXT, resume.REACHED) for stop in report.stops):
        return exit_codes.PRECONDITION
    if report.skipped and not report.stops:
        return exit_codes.LOCKED
    return exit_codes.OK


def _continue(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    decide = confirming.terminal(app, invocation.stdin, invocation.stdout) if invocation.interactive else None
    result = orchestrator(app).continue_(
        args.subjects, args.select, until=Stage(args.until) if args.until else None,
        from_=Stage(args.from_stage) if args.from_stage else None, interactive=invocation.interactive,
        confirm=decide)
    report = result.report
    lines = [f"{item.ref.id}：{item.command} → {item.message}" for item in report.progress]
    lines += [f"{stop.ref.id} 停在「{stop.status}」：{stop.reason}" for stop in report.stops]
    lines += [f"{subject} 跳过：{reason}" for subject, reason in report.skipped]
    code = _continue_code(report)
    lines.insert(0, {exit_codes.OK: "已完成", exit_codes.GATE: "停在需要用户的关口",
                     exit_codes.FAILED: "有步骤失败", exit_codes.LOCKED: "对象正被其他运行处理"}
                 .get(code, "前置条件不满足"))
    first = report.stops[0] if report.stops else None
    pending = [confirming.pending_dict(confirming.load(app, stop.operation)) for stop in report.stops
               if stop.operation]
    values = {"runId": result.run.id, "progress": [{"subject": item.ref.to_dict(), "command": item.command,
                                                    "message": item.message} for item in report.progress],
              "stops": [stop.to_dict() for stop in report.stops],
              "skipped": [{"subject": subject, "reason": reason} for subject, reason in report.skipped]}
    errors = [error("StepFailed", stop.reason) for stop in report.stops if stop.failed]
    return Outcome("continue", code, lines, first.ref.to_dict() if first else None, values,
                   first.to_dict() if first else None, first.command if first else None, pending, errors)


def _date(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as failure:
        raise UsageError(f"日期须为 YYYY-MM-DD：{text}") from failure


def _find(invocation: Any) -> Outcome:
    args = invocation.args
    found = orchestrator(invocation.app).find(args.text, since=_date(args.since), until=_date(args.until),
                                              kind=args.type)
    lines = [f"找到 {len(found)} 个候选" if found else "没有匹配的对象，换一种描述或直接给出编号"]
    lines += [f"- {item.ref.id} [{item.status}] {item.title}(匹配：{'、'.join(item.matched)})" for item in found]
    code = exit_codes.GATE if len(found) > 1 else exit_codes.OK
    stopped = {"gate": resume.CANDIDATE_CHOICE, "candidates": len(found)} if len(found) > 1 else None
    return Outcome("find", code, lines, result=[item.to_dict() for item in found], stopped_at=stopped)


def _pending(invocation: Any) -> Outcome:
    app = invocation.app
    stage = Stage(invocation.args.stage) if invocation.args.stage else None
    found = [PendingOperation.from_record(record)
             for record in pending_operations.find(app.conn, status=OperationStatus.PENDING)
             if stage is None or record.stage is stage]
    items = [confirming.pending_dict(operation) for operation in found]
    lines = [f"待确认操作 {len(items)} 项"]
    return Outcome("pending", exit_codes.OK, lines, result=items, pending=items)


def _state(operation: PendingOperation) -> dict[str, Any]:
    return {"id": operation.id, "kind": operation.kind.value, "status": operation.status.value,
            "confirmationsGiven": operation.confirmations_given,
            "result": None if operation.result is None else dict(operation.result)}


def _confirm(invocation: Any) -> Outcome:
    app = invocation.app
    operation_id = invocation.args.operation
    result = confirming.confirm(app, operation_id)
    if isinstance(result, PendingOperation):
        message = f"{operation_id} 已确认 {result.confirmations_given} 次，还需再确认一次"
        return Outcome("confirm", exit_codes.GATE, [message], {"type": "operation", "id": operation_id},
                       _state(result), pending=[confirming.pending_dict(result)])
    assert isinstance(result, OperationResult)
    operation = result.operation
    values = _state(operation)
    subject = {"type": "operation", "id": operation_id}
    if operation.status is OperationStatus.EXECUTED:
        return Outcome("confirm", exit_codes.OK, [f"{operation_id} 已执行"], subject, values)
    if operation.status is OperationStatus.EXPIRED:
        message = f"{operation_id} 的前置条件已变化，已标为过期；重新运行生成它的命令"
        return Outcome("confirm", exit_codes.PRECONDITION, [message], subject, values,
                       errors=[error("OperationExpired", message)])
    message = f"{operation_id} 执行失败：{result.error}"
    return Outcome("confirm", exit_codes.FAILED, [message], subject, values,
                   errors=[error(type(result.error).__name__, str(result.error))])


def _reject(invocation: Any) -> Outcome:
    operation = confirming.reject(invocation.app, invocation.args.operation, invocation.args.note)
    return Outcome("reject", exit_codes.OK, [f"{operation.id} 已拒绝"], {"type": "operation", "id": operation.id},
                   _state(operation))


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    run = leaf(commands, common, "run", _run, "按时间表与状态推进一轮")
    run.add_argument("--scheduled", action="store_true", help="由定时任务触发")
    run.add_argument("--subject", help="链选择的对象(问题编号)")
    run.add_argument("--probe", choices=[probe.value for probe in ProbeKind], help="链中 collect 的探针")
    run.add_argument("--level", choices=[level.value for level in ProbeLevel], help="链中 collect 的档位")
    leaf(commands, common, "tick", _tick, "定时唤醒：固定时刻完整运行，其余时刻只检查事件(新提交、新部署)")
    paused = leaf(commands, common, "pause", _pause, "暂停：不发起新的运行(不带 --workspace 为全局)")
    paused.add_argument("--note", help="暂停的说明")
    leaf(commands, common, "resume", _resume, "恢复(不带 --workspace 为全局)")
    leaf(commands, common, "status", _status, "全局状态、暂停与接入状态、待用户处理的事项与推荐做法")
    nxt = leaf(commands, common, "next", _next, "对象的当前状态与下一步，不执行")
    nxt.add_argument("subjects", nargs="+", help="问题或 Issue 编号")
    cont = leaf(commands, common, "continue", _continue, "按状态表继续，直到终点或下一个关口")
    cont.add_argument("subjects", nargs="*", help="问题或 Issue 编号")
    cont.add_argument("--until", choices=UNTIL_CHOICES, help="做到哪个模块为止")
    cont.add_argument("--from", dest="from_stage", choices=FROM_CHOICES, help="从哪一步重来")
    find = leaf(commands, common, "find", _find, "按描述查找问题与 Issue 的候选")
    find.add_argument("text", help="标题、接口路由、页面或文件名的一部分")
    find.add_argument("--since", help="起始日期 YYYY-MM-DD")
    find.add_argument("--until", help="结束日期 YYYY-MM-DD")
    find.add_argument("--type", choices=[resume.PROBLEM, resume.ISSUE], help="只找问题或只找 Issue")
    pending = leaf(commands, common, "pending", _pending, "列出待确认操作")
    pending.add_argument("--stage", choices=[stage.value for stage in Stage], help="只列这个模块的")
    confirm = leaf(commands, common, "confirm", _confirm, "确认并执行一个待确认操作")
    confirm.add_argument("operation", help="操作编号 OP-0001")
    reject = leaf(commands, common, "reject", _reject, "拒绝一个待确认操作")
    reject.add_argument("operation", help="操作编号 OP-0001")
    reject.add_argument("--note", help="拒绝的说明")
