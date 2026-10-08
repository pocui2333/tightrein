"""`tightrein run [阶段] [--object <编号>] [--dry-run] [--debug]`：手动触发一次运行(protocol/schedule)。

不带阶段按状态推进一轮(原 continue 并入)；带阶段只跑该阶段；--object 只推进一个对象；--dry-run 只列出会做什么，
不取运行锁、不写记录；--debug 让成功的模型调用也保存 prompt 与 raw(AgentContext.debug)。launchd 定时调用时带 `--trigger schedule`(不在帮助中列出)。
退出码：被拒绝(锁被占、暂停或急停、额度停机)3；有步骤失败 1；停在人工关卡 4。
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from tightrein.cli import exit_codes
from tightrein.cli.session import Result, Session, add_command, subject_id
from tightrein.cli.text import text
from tightrein.protocol import schedule
from tightrein.protocol.naming import STAGES


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    parser = add_command(commands, "run", common=common, language=language, help_key="help.run", handler=handle)
    parser.add_argument("stage", nargs="?", choices=STAGES, metavar="<stage>", help=text(language, "help.run_stage"))
    parser.add_argument("--object", dest="subject", metavar="<id>", help=text(language, "help.run_object"))
    parser.add_argument("--dry-run", action="store_true", help=text(language, "help.option_dry_run"))
    parser.add_argument("--debug", action="store_true", help=text(language, "help.run_debug"))
    parser.add_argument("--trigger", choices=schedule.TRIGGERS, default="manual", help=argparse.SUPPRESS)


def handle(session: Session) -> Result:
    args = session.args
    subject = subject_id(args.subject) if args.subject else None
    current = session.runtime(args.stage or "run")
    if args.debug:  # 成功的模型调用也保存 prompt 与 raw(重新录制整体测试、排查提示时用)
        current.agents = replace(current.agents, debug=True)
    outcome = schedule.run(current, trigger=args.trigger, stage=args.stage, subject=subject, dry_run=args.dry_run,
                           host=session.externals.host, alive=session.externals.alive)
    data = {"run": outcome.run, "status": outcome.status.value, "reason": outcome.reason, "halted": outcome.halted,
            "steps": [vars(step) | {"status": step.status.value} for step in outcome.steps],
            "planned": [vars(item) for item in outcome.planned]}
    return Result(session.command, _code(outcome), _lines(session, outcome), data, _next(outcome))


def _code(outcome: schedule.Outcome) -> int:
    if outcome.status is schedule.RunStatus.REFUSED:
        return exit_codes.REFUSED
    if outcome.failed:
        return exit_codes.FAILED
    if outcome.pending:
        return exit_codes.GATE
    return exit_codes.OK


def _lines(session: Session, outcome: schedule.Outcome) -> list[str]:
    if outcome.status in (schedule.RunStatus.REFUSED, schedule.RunStatus.SKIPPED):
        return [session.text(f"cmd.run_{outcome.status.value}", reason=outcome.reason)]
    if session.args.dry_run:
        return [session.text("cmd.run_plan", count=len(outcome.planned))] + [
            f"  {item.stage}  {item.subject or '-'}  {item.reason}" for item in outcome.planned]
    lines = [session.text("cmd.run_done", run=outcome.run, count=len(outcome.steps))]
    lines += [f"  {step.stage}  {step.subject or '-'}  {session.text('cmd.step_' + step.status.value)}  {step.summary}"
              for step in outcome.steps]
    if outcome.halted:
        lines.append(session.text("cmd.run_halted", reason=outcome.halted))
    return lines


def _next(outcome: schedule.Outcome) -> str | None:
    pending = next((step.subject for step in outcome.steps if step.status is schedule.StepStatus.PENDING), None)
    if pending is not None:
        return f"tightrein show {pending} --doc pending"
    failed = next((step.subject for step in outcome.steps
                   if step.status is schedule.StepStatus.FAILED and step.subject), None)
    return f"tightrein show {failed} --doc failure" if failed else None
