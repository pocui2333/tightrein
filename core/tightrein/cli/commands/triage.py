"""分诊：triage、triage queue、retriage(含改判)。"""

from __future__ import annotations

import argparse
from typing import Any

from tightrein.cli import exit_codes, selectors
from tightrein.cli.commands.common import leaf
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.domain.enums import Disposition, Severity, Verdict
from tightrein.pipeline.triage.service import TriageRequest, TriageRun


def triage_outcome(name: str, app: Any, run: TriageRun) -> Outcome:
    if run.plan is not None:
        lines = [f"将分诊 {len(run.plan)} 个问题", *(f"- {item.problem_id} {item.title}：{item.role}" for item in run.plan)]
        return Outcome(name, exit_codes.OK, lines, result=[
            {"problemId": item.problem_id, "title": item.title, "role": item.role} for item in run.plan])
    lines = [run.summary or run.message or "没有需要分诊的问题"]
    lines += [f"- {item.problem_id}「{item.title}」：{item.status.label}"
              + (f"，{item.verdict.label}，去向 {item.disposition.label}" if item.verdict and item.disposition else "")
              + (f"({item.reason})" if item.reason else "") for item in run.items]
    lines += [f"- {problem_id} 跳过：{reason}" for problem_id, reason in run.skipped]
    items = [{"problemId": item.problem_id, "status": item.status.value,
              "verdict": item.verdict.value if item.verdict else None,
              "severity": item.severity.value if item.severity else None,
              "disposition": item.disposition.value if item.disposition else None,
              "handoff": app.layout.relative(item.handoff) if item.handoff else None,
              "findings": app.layout.relative(item.findings) if item.findings else None,
              "mergedInto": item.merged_into, "reason": item.reason} for item in run.items]
    code = run.exit_code
    if code == exit_codes.OK and not run.items and run.skipped:
        code = exit_codes.PRECONDITION
    subject = {"type": "run", "id": run.run.id} if run.run else None
    return Outcome(name, code, lines, subject, {"items": items, "skipped": [
        {"problemId": problem_id, "reason": reason} for problem_id, reason in run.skipped]})


def _triage(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    chosen = selectors.problem_ids(app.conn, args.select)
    request = TriageRequest(chosen, args.limit, args.commit, args.input, args.ignore_state, args.dry_run,
                            invocation.overrides)
    return triage_outcome("triage", app, app.triage().run(request))


def _queue(invocation: Any) -> Outcome:
    found = invocation.app.triage().queue()
    lines = [f"人工队列 {len(found)} 个问题"]
    lines += [f"- {item.problem_id}「{item.title}」({item.verdict.label})缺少：{'、'.join(item.missing_info) or '无'}"
              for item in found]
    values = [{"problemId": item.problem_id, "title": item.title, "verdict": item.verdict.value,
               "missingInfo": list(item.missing_info), "findings": item.findings} for item in found]
    return Outcome("triage queue", exit_codes.OK, lines, result=values)


def _retriage(invocation: Any) -> Outcome:
    args = invocation.args
    app = invocation.app
    if args.verdict is None:
        if args.severity or args.disposition:
            raise UsageError("--severity 与 --disposition 只用于改判(--verdict)")
        return triage_outcome("retriage", app, app.triage().retriage(args.problem, args.note, invocation.overrides))
    if not args.reason:
        raise UsageError("改判需要 --reason")
    run = app.triage().override(args.problem, Verdict(args.verdict), args.reason,
                                severity=Severity(args.severity) if args.severity else None,
                                chosen=Disposition(args.disposition) if args.disposition else None)
    return triage_outcome("retriage", app, run)


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    triage = commands.add_parser("triage", parents=[common.dry_run], help="分诊新发现与回归的问题")
    triage.set_defaults(handler=_triage, command_name="triage")
    triage.add_argument("--limit", type=int, help="本次最多分诊几个问题")
    sub = triage.add_subparsers(dest="triage_command", parser_class=type(triage))
    leaf(sub, common, "queue", _queue, "人工队列中的问题")


def register_problem(commands: Any, common: argparse.ArgumentParser) -> None:
    retriage = leaf(commands, common, "retriage", _retriage, "重新分诊或改判一个问题")
    retriage.add_argument("problem")
    retriage.add_argument("--note", help="补充信息")
    retriage.add_argument("--verdict", choices=[item.value for item in Verdict], help="改判的结论")
    retriage.add_argument("--reason", help="改判的理由")
    retriage.add_argument("--severity", choices=[item.value for item in Severity])
    retriage.add_argument("--disposition", choices=[item.value for item in Disposition])

