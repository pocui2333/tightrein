"""运行控制(protocol/recovery.md「运行控制命令」)：pause、stop、resume、take、give。

- pause：当前这一步做完后停下，不再开始新的步骤与对象；
- stop：急停：给本机进行中运行的进程发 SIGTERM，那边的入口转成中断、收尾标为中断，下次从检查点接着做；
- resume：从暂停或急停恢复；
- take / give：手动接管一个 Issue(tightrein 不再碰它)、交还后从检查点接着做。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from tightrein.cli.exit_codes import UsageError
from tightrein.cli.session import Result, Session, add_command, subject_id
from tightrein.cli.text import text
from tightrein.protocol import recovery
from tightrein.protocol.naming import kind_of

type Parsers = argparse._SubParsersAction[argparse.ArgumentParser]
DEFAULT_USER = "user"


def register_pause(commands: Parsers, common: argparse.ArgumentParser, language: str) -> None:
    parser = add_command(commands, "pause", common=common, language=language, help_key="help.pause", handler=pause)
    parser.add_argument("--note", help=text(language, "help.option_note"))


def register_stop(commands: Parsers, common: argparse.ArgumentParser, language: str) -> None:
    parser = add_command(commands, "stop", common=common, language=language, help_key="help.stop", handler=stop)
    parser.add_argument("--note", help=text(language, "help.option_note"))


def register_resume(commands: Parsers, common: argparse.ArgumentParser, language: str) -> None:
    add_command(commands, "resume", common=common, language=language, help_key="help.resume", handler=resume)


def register_take(commands: Parsers, common: argparse.ArgumentParser, language: str) -> None:
    parser = add_command(commands, "take", common=common, language=language, help_key="help.take", handler=take)
    parser.add_argument("subject", metavar="<id>", help=text(language, "help.arg_issue"))


def register_give(commands: Parsers, common: argparse.ArgumentParser, language: str) -> None:
    parser = add_command(commands, "give", common=common, language=language, help_key="help.give", handler=give)
    parser.add_argument("subject", metavar="<id>", help=text(language, "help.arg_issue"))


REGISTERS: tuple[Callable[[Parsers, argparse.ArgumentParser, str], None], ...] = (
    register_pause, register_stop, register_resume, register_take, register_give)


def pause(session: Session) -> Result:
    if not session.confirm([session.text("cmd.pause_action")]):
        return session.declined()
    pause_now(session, note=session.args.note)
    return Result(session.command, lines=[session.text("cmd.paused")], next="tightrein resume")


def stop(session: Session) -> Result:
    if not session.confirm([session.text("cmd.stop_action")]):
        return session.declined()
    stopped = stop_now(session, note=session.args.note)
    lines = [session.text("cmd.stopped", count=len(stopped))] + [f"  {run}" for run in stopped]
    return Result(session.command, lines=lines, data={"stopped": stopped}, next="tightrein resume")


def resume(session: Session) -> Result:
    if recovery.control(session.layout) is None:
        return Result(session.command, lines=[session.text("cmd.not_paused")])
    if not session.confirm([session.text("cmd.resume_action")]):
        return session.declined()
    previous = recovery.resume(session.layout)
    mode = previous.mode.value if previous is not None else None
    return Result(session.command, lines=[session.text("cmd.resumed")], data={"previous": mode}, next="tightrein run")


def take(session: Session) -> Result:
    subject = _issue(session.args.subject)
    if not session.confirm([session.text("cmd.take_action", subject=subject)]):
        return session.declined()
    by = session.externals.environ.get("USER") or DEFAULT_USER
    _event(session, subject, "TAKE", by)
    return Result(session.command, lines=[session.text("cmd.taken", subject=subject, by=by)],
                  next=f"tightrein give {subject}")


def give(session: Session) -> Result:
    subject = _issue(session.args.subject)
    if not session.confirm([session.text("cmd.give_action", subject=subject)]):
        return session.declined()
    _event(session, subject, "GIVE", None)
    return Result(session.command, lines=[session.text("cmd.given", subject=subject)],
                  next=f"tightrein run --object {subject}")


def pause_now(session: Session, *, note: str | None) -> None:
    recovery.pause(session.layout, session.externals.clock, note)


def stop_now(session: Session, *, note: str | None) -> list[str]:
    externals = session.externals
    return recovery.stop(session.layout, session.workspace.conn, externals.clock, host=externals.host,
                         terminate=externals.terminate, note=note)


def _event(session: Session, subject: str, event: str, reason: str | None) -> None:
    """接管与交还走 Issue 状态机(held 状态、held_by 与历史一起改)，不只改 held_by。"""
    from tightrein.assess.issue.transitions import IssueEvent, apply_event

    apply_event(session.runtime("run"), subject, IssueEvent[event], reason=reason)


def _issue(value: str) -> str:
    subject = subject_id(value)
    if kind_of(subject) != "issue":
        raise UsageError(f"只能接管或交还 Issue：{value}")
    return subject
