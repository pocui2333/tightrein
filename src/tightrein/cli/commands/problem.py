"""`tightrein problem …`：查看与人工处置问题(去重后的结果)。

- list [--status <状态>]；
- mute <编号> --days N --note "<原因>"：抑制 N 天(到期、再出现或出现在新版本时由去重恢复，见 collect/dedup/status.py)；
- unmute <编号>：取消抑制，回到待评估；
- reopen <编号>：已关闭或已解决的重新打开，回到待评估；
- retriage <编号> [--note "<补充的信息>"]：请求重新评估(去掉转人工标记)，下一次运行评估；带补充信息的重新取证，
  不带的复用采集时的取证；
- override <编号> --verdict <判定> --reason "<理由>" [--severity] [--destination]：改判，不调用模型。
人工处置写进 problems 表的 extra(带原因与时间)，供重建与复盘。
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import timedelta
from typing import Any

from tightrein.cli.exit_codes import UsageError
from tightrein.cli.session import Result, Session, add_command, add_group, subject_id
from tightrein.cli.text import text
from tightrein.collect.dedup.status import IGNORE, ProblemStatus, ignore_condition
from tightrein.protocol.naming import format_iso, kind_of
from tightrein.store.tables import problems

MANUAL = "manual"  # problems.extra 中人工处置的记录
VERDICTS = ("confirmed", "conditional", "refuted", "insufficient")
SEVERITIES = ("P0", "P1", "P2", "P3")
DESTINATIONS = ("issue", "false_positive", "wont_fix", "tradeoff", "watch", "watch_evidence", "manual",
                "awaiting_deploy")
REOPENABLE = frozenset({ProblemStatus.CLOSED, ProblemStatus.RESOLVED})
NOT_MUTABLE = frozenset({ProblemStatus.MUTED, ProblemStatus.CLOSED})
STAGE = "assess"


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    group = add_group(commands, "problem", language=language, help_key="help.problem")
    parser = add_command(group, "list", common=common, language=language, help_key="help.problem_list",
                         handler=list_)
    parser.add_argument("--status", choices=[status.value for status in ProblemStatus],
                        help=text(language, "help.problem_status"))
    parser = add_command(group, "mute", common=common, language=language, help_key="help.problem_mute",
                         handler=mute)
    parser.add_argument("subject", metavar="<id>", help=text(language, "help.arg_problem"))
    parser.add_argument("--days", type=int, required=True, help=text(language, "help.problem_days"))
    parser.add_argument("--note", required=True, help=text(language, "help.option_note"))
    for name, handler in (("unmute", unmute), ("reopen", reopen)):
        parser = add_command(group, name, common=common, language=language, help_key=f"help.problem_{name}",
                             handler=handler)
        parser.add_argument("subject", metavar="<id>", help=text(language, "help.arg_problem"))
        parser.add_argument("--note", help=text(language, "help.option_note"))
    parser = add_command(group, "retriage", common=common, language=language, help_key="help.problem_retriage",
                         handler=retriage)
    parser.add_argument("subject", metavar="<id>", help=text(language, "help.arg_problem"))
    parser.add_argument("--note", help=text(language, "help.retriage_note"))
    parser = add_command(group, "override", common=common, language=language, help_key="help.problem_override",
                         handler=override)
    parser.add_argument("subject", metavar="<id>", help=text(language, "help.arg_problem"))
    parser.add_argument("--verdict", required=True, choices=VERDICTS, help=text(language, "help.override_verdict"))
    parser.add_argument("--reason", required=True, help=text(language, "help.override_reason"))
    parser.add_argument("--severity", choices=SEVERITIES, help=text(language, "help.override_severity"))
    parser.add_argument("--destination", choices=DESTINATIONS, help=text(language, "help.override_destination"))


def list_(session: Session) -> Result:
    found = problems.find(session.workspace.conn, status=session.args.status)
    items = [{"id": item.id, "status": item.status, "source": item.source, "count": item.count,
              "lastSeen": format_iso(item.last_seen), "title": item.title, "issue": item.issue} for item in found]
    lines = [session.text("cmd.problem_count", count=len(items))] + [
        f"  {item['id']}  {item['status']}  {item['source']}  ×{item['count']}  {item['title']}" for item in items]
    return Result(session.command, lines=lines, data=items)


def mute(session: Session) -> Result:
    days = session.args.days
    if days < 1:
        raise UsageError(session.text("cmd.days_positive"))
    problem = _problem(session)
    if ProblemStatus(problem.status) in NOT_MUTABLE:
        raise UsageError(session.text("cmd.problem_wrong_status", subject=problem.id, status=problem.status))
    if not session.confirm([session.text("cmd.mute_action", subject=problem.id, days=days)]):
        return session.declined()
    until = session.externals.clock.now() + timedelta(days=days)
    condition = ignore_condition(problem, until=until)
    extra = {**problem.extra, IGNORE: condition.to_json(), MANUAL: _manual(session, "mute", session.args.note)}
    return _save(session, replace(problem, status=ProblemStatus.MUTED.value, muted_until=until, extra=extra))


def unmute(session: Session) -> Result:
    problem = _problem(session)
    if problem.status != ProblemStatus.MUTED:
        raise UsageError(session.text("cmd.problem_wrong_status", subject=problem.id, status=problem.status))
    if not session.confirm([session.text("cmd.unmute_action", subject=problem.id)]):
        return session.declined()
    extra = {key: value for key, value in problem.extra.items() if key != IGNORE}
    extra[MANUAL] = _manual(session, "unmute", session.args.note)
    return _save(session, replace(problem, status=ProblemStatus.NEW.value, muted_until=None, extra=extra))


def reopen(session: Session) -> Result:
    problem = _problem(session)
    if ProblemStatus(problem.status) not in REOPENABLE:
        raise UsageError(session.text("cmd.problem_wrong_status", subject=problem.id, status=problem.status))
    if not session.confirm([session.text("cmd.reopen_action", subject=problem.id)]):
        return session.declined()
    extra = {**problem.extra, MANUAL: _manual(session, "reopen", session.args.note)}
    return _save(session, replace(problem, status=ProblemStatus.NEW.value, extra=extra))


def retriage(session: Session) -> Result:
    from tightrein.assess import assess  # 评估阶段；延迟导入，别的命令不必加载评估

    problem = _problem(session)
    if not session.confirm([session.text("cmd.retriage_action", subject=problem.id)]):
        return session.declined()
    try:
        found = assess.request_retriage(session.runtime(STAGE), problem.id, session.args.note)
    except ValueError as error:
        raise UsageError(session.text("cmd.problem_wrong_status", subject=problem.id, status=problem.status)) \
            from error
    return Result(session.command, lines=[session.text("cmd.retriage_requested", subject=found.id)],
                  data={"id": found.id, "status": found.status}, next=f"tightrein run assess --object {found.id}")


def override(session: Session) -> Result:
    from tightrein.assess import assess

    problem = _problem(session)
    args = session.args
    if not session.confirm([session.text("cmd.override_action", subject=problem.id, verdict=args.verdict)]):
        return session.declined()
    current = session.begin(STAGE)
    try:
        outcome = assess.override(current, problem.id, args.verdict, args.reason, severity=args.severity,
                                  destination=args.destination)
    except ValueError as error:
        raise UsageError(str(error)) from error
    session.end(current)
    lines = [session.text("cmd.override_done", subject=problem.id, verdict=outcome.verdict,
                          destination=outcome.destination)]
    return Result(session.command, lines=lines,
                  data={"id": problem.id, "verdict": outcome.verdict, "destination": outcome.destination,
                        "issue": outcome.issue},
                  next=f"tightrein show {outcome.issue or problem.id}")


def _problem(session: Session) -> problems.Problem:
    subject = subject_id(session.args.subject)
    if kind_of(subject) != "problem":
        raise UsageError(session.text("cmd.problem_only", subject=subject))
    found = problems.get(session.workspace.conn, subject)
    if found is None:
        raise LookupError(session.text("cmd.no_subject", subject=subject))
    return found


def _manual(session: Session, action: str, note: str | None) -> dict[str, Any]:
    return {"action": action, "note": note, "at": format_iso(session.externals.clock.now()),
            "by": session.externals.environ.get("USER")}


def _save(session: Session, problem: problems.Problem) -> Result:
    problems.save(session.workspace.conn, problem, session.externals.clock)
    return Result(session.command, lines=[session.text("cmd.problem_saved", subject=problem.id, status=problem.status)],
                  data={"id": problem.id, "status": problem.status}, next=f"tightrein show {problem.id}")
