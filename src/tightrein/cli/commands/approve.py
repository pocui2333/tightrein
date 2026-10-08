"""`tightrein approve <编号> [--option <序号>] [--note "<补充>"]`、`tightrein reject <编号> --note "<原因>"`。

审核停在关卡上的 Issue：
- 立项放行(needs_decision，还没进入实施)：按 Issue 状态机(assess/issue/transitions.apply_event)走 APPROVE 或
  REJECT(不修)，历史与 GitHub 同步由状态机做；
- 实施中的关卡(定案等，issues.gate 有值)或实施中停下等用户(needs_decision 且 stage 为实施)：用户的决定(通过可带
  选项序号，不通过必带原因)经 implement/approve 的 record 持久化(写记录文件，重建不丢)，清掉 gate；停下的再经
  APPROVE 回到继续实施。不通过不取消 Issue：停着的那一步按原因重出(计划「reject：方案按原因重出」)。
审核本身不推进对象，推进交给 `tightrein run --object <编号>`。
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from tightrein.cli.exit_codes import UsageError
from tightrein.cli.session import Result, Session, add_command, subject_id
from tightrein.cli.text import text
from tightrein.protocol.naming import format_iso, kind_of
from tightrein.store.tables import issues

NEEDS_DECISION = "needs_decision"
IMPLEMENT = "implement"
APPROVE, REJECT = "approve", "reject"
DONE_KEYS = {APPROVE: "cmd.approved", REJECT: "cmd.rejected"}


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    parser = add_command(commands, "approve", common=common, language=language, help_key="help.approve",
                         handler=approve)
    parser.add_argument("subject", metavar="<id>", help=text(language, "help.arg_issue"))
    parser.add_argument("--option", type=int, metavar="<n>", help=text(language, "help.approve_option"))
    parser.add_argument("--note", help=text(language, "help.approve_note"))


def register_reject(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
                    language: str) -> None:
    parser = add_command(commands, "reject", common=common, language=language, help_key="help.reject",
                         handler=reject)
    parser.add_argument("subject", metavar="<id>", help=text(language, "help.arg_issue"))
    parser.add_argument("--note", required=True, help=text(language, "help.reject_note"))


def approve(session: Session) -> Result:
    return _decide(session, APPROVE, option=session.args.option, note=session.args.note)


def reject(session: Session) -> Result:
    if not session.args.note.strip():
        raise UsageError(session.text("cmd.reject_needs_note"))
    return _decide(session, REJECT, option=None, note=session.args.note)


def _decide(session: Session, verdict: str, *, option: int | None, note: str | None) -> Result:
    subject = subject_id(session.args.subject)
    if kind_of(subject) != "issue":
        raise UsageError(session.text("cmd.approve_issue_only", subject=subject))
    conn, clock = session.workspace.conn, session.externals.clock
    issue = issues.get(conn, subject)
    if issue is None:
        raise LookupError(session.text("cmd.no_subject", subject=subject))
    if issue.status != NEEDS_DECISION and issue.gate is None:
        raise UsageError(session.text("cmd.not_waiting", subject=subject, status=issue.status))
    point = issue.gate or NEEDS_DECISION
    if not session.confirm([session.text(f"cmd.{verdict}_action", subject=subject, point=point)]):
        return session.declined()
    from tightrein.assess.issue import files as issue_files
    from tightrein.assess.issue.transitions import IssueEvent, apply_event
    from tightrein.implement.approve.approve import record
    from tightrein.implement.context import Decision

    if issue.status == NEEDS_DECISION and issue.stage != IMPLEMENT:
        event = IssueEvent.APPROVE if verdict == APPROVE else IssueEvent.REJECT
        updated = apply_event(session.runtime("assess"), subject, event, reason=note, note=note)
    else:
        runtime = session.runtime(IMPLEMENT)
        record(runtime, subject, Decision(issue.step or point, verdict, option, note, format_iso(clock.now())))
        if issue.status == NEEDS_DECISION:
            updated = apply_event(runtime, subject, IssueEvent.APPROVE, reason=note, note=note)
        else:
            current = issues.get(conn, subject)
            assert current is not None
            updated = replace(current, gate=None)
            issue_files.write(runtime, updated)
    return Result(session.command, lines=[session.text(DONE_KEYS[verdict], subject=subject, point=point)],
                  data={"subject": subject, "point": point, "verdict": verdict, "option": option, "note": note,
                        "status": updated.status},
                  next=f"tightrein run --object {subject}")
