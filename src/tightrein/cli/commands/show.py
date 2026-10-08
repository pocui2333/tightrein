"""`tightrein show <编号> [--steps] [--doc pending|failure|deliver]`、`tightrein show --search "<关键词>"`。

一个问题或 Issue 的详情：当前状态、每一步的结论(--steps 加上量化数据)、三种给人看的文档的路径；--doc 直接打印
该文档。问题与 Issue 编号不重叠，按编号自动识别。只读 store 与交接文件。
"""

from __future__ import annotations

import argparse
from typing import Any

from tightrein.cli.exit_codes import UsageError
from tightrein.cli.session import Result, Session, add_command, subject_id
from tightrein.cli.text import text
from tightrein.protocol import recovery
from tightrein.protocol.handoff import Handoff
from tightrein.protocol.naming import HUMAN_DOCUMENTS, format_count, format_duration, format_iso, kind_of
from tightrein.store.tables import issues, occurrences, problems

SEARCH_LIMIT = 20


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    parser = add_command(commands, "show", common=common, language=language, help_key="help.show", handler=handle)
    parser.add_argument("subject", nargs="?", metavar="<id>", help=text(language, "help.arg_subject"))
    parser.add_argument("--steps", action="store_true", help=text(language, "help.show_steps"))
    parser.add_argument("--doc", choices=HUMAN_DOCUMENTS, help=text(language, "help.show_doc"))
    parser.add_argument("--search", metavar="<text>", help=text(language, "help.show_search"))


def handle(session: Session) -> Result:
    args = session.args
    if args.search is not None:
        if args.subject is not None:
            raise UsageError(session.text("cmd.show_search_or_id"))
        return search(session, args.search)
    if args.subject is None:
        raise UsageError(session.text("cmd.show_needs_id"))
    subject = subject_id(args.subject)
    if args.doc is not None:
        return document(session, subject, args.doc)
    return detail(session, subject, steps=args.steps)


def detail(session: Session, subject: str, *, steps: bool) -> Result:
    layout = session.layout
    record = _record(session, subject)
    checkpoints = recovery.checkpoints(layout, subject)
    documents = {name: str(path) for name in HUMAN_DOCUMENTS
                 if (path := layout.human_document(subject, name)).is_file()}
    lines = [_headline(session, subject, record)]
    for item in checkpoints:
        lines.append(_step_line(item.handoff))
        if steps:
            lines.append("      " + _metrics_line(item.handoff))
    lines += [session.text("cmd.show_document", kind=name, path=path) for name, path in documents.items()]
    data = {"subject": subject, "kind": kind_of(subject), "record": record,
            "steps": [item.handoff.to_json() for item in checkpoints], "documents": documents}
    return Result(session.command, lines=lines, data=data, next=_next(subject, record, documents))


def document(session: Session, subject: str, kind: str) -> Result:
    path = session.layout.human_document(subject, kind)
    if not path.is_file():
        raise LookupError(session.text("cmd.show_no_document", subject=subject, kind=kind))
    content = path.read_text(encoding="utf-8")
    return Result(session.command, lines=[content.rstrip("\n")], data={"path": str(path), "content": content})


def search(session: Session, words: str) -> Result:
    """按标题与位置查找问题与 Issue(原 find)；不区分大小写，最多列出 SEARCH_LIMIT 条。"""
    conn = session.workspace.conn
    pattern = f"%{words}%"
    found_problems = conn.execute(
        "SELECT id FROM problems WHERE title LIKE ? OR location LIKE ? ORDER BY last_seen DESC LIMIT ?",
        (pattern, pattern, SEARCH_LIMIT)).fetchall()
    found_issues = conn.execute(
        "SELECT id FROM issues WHERE title LIKE ? ORDER BY id DESC LIMIT ?", (pattern, SEARCH_LIMIT)).fetchall()
    items = [_brief(session, row[0]) for row in found_issues] + [_brief(session, row[0]) for row in found_problems]
    lines = [session.text("cmd.show_found", count=len(items), words=words)]
    lines += [f"  {item['id']}  {item['status']}  {item['title']}" for item in items]
    return Result(session.command, lines=lines, data=items,
                  next=f"tightrein show {items[0]['id']}" if len(items) == 1 else None)


def _record(session: Session, subject: str) -> dict[str, Any]:
    conn = session.workspace.conn
    kind = kind_of(subject)
    if kind == "issue":
        issue = issues.get(conn, subject)
        if issue is None:
            raise LookupError(session.text("cmd.no_subject", subject=subject))
        return {"title": issue.title, "status": issue.status, "severity": issue.severity, "kind": issue.kind,
                "origin": issue.origin, "gate": issue.gate, "stage": issue.stage, "step": issue.step,
                "round": issue.round, "branch": issue.branch, "pr": issue.pr, "heldBy": issue.held_by}
    if kind == "problem":
        problem = problems.get(conn, subject)
        if problem is None:
            raise LookupError(session.text("cmd.no_subject", subject=subject))
        return {"title": problem.title, "status": problem.status, "source": problem.source,
                "checkType": problem.check_type, "location": problem.location, "count": problem.count,
                "firstSeen": format_iso(problem.first_seen), "lastSeen": format_iso(problem.last_seen),
                "issue": problem.issue, "occurrences": len(occurrences.find(conn, subject))}
    raise UsageError(session.text("cmd.show_not_object", subject=subject))


def _brief(session: Session, subject: str) -> dict[str, Any]:
    record = _record(session, subject)
    return {"id": subject, "status": record["status"], "title": record["title"]}


def _headline(session: Session, subject: str, record: dict[str, Any]) -> str:
    where = ".".join(str(part) for part in (record.get("stage"), record.get("step")) if part)
    parts = [subject, record["status"], record.get("severity") or "", where, record["title"]]
    line = "  ".join(part for part in parts if part)
    if record.get("heldBy"):
        line += "  " + session.text("cmd.show_held", by=record["heldBy"])
    return line


def _step_line(handoff: Handoff) -> str:
    round_ = f".r{handoff.round}" if handoff.round is not None else ""
    return f"  {handoff.point}{round_}  {handoff.status.value}  {handoff.summary}"


def _metrics_line(handoff: Handoff) -> str:
    metrics = handoff.metrics
    parts = []
    if metrics.duration_ms is not None:
        parts.append(format_duration(metrics.duration_ms / 1000))
    if metrics.calls is not None:
        parts.append(f"calls {metrics.calls}")
    if metrics.tokens is not None:
        parts.append(f"tokens {format_count(metrics.tokens.input + metrics.tokens.output)}")
    if metrics.files_changed is not None:
        parts.append(f"files {metrics.files_changed}")
    if metrics.lines_changed is not None:
        parts.append(f"lines {metrics.lines_changed}")
    return "  ".join(parts) or "-"


def _next(subject: str, record: dict[str, Any], documents: dict[str, str]) -> str | None:
    if record.get("gate") or record.get("status") == "needs_decision":
        return f"tightrein approve {subject}"
    if "pending" in documents:
        return f"tightrein show {subject} --doc pending"
    if "failure" in documents:
        return f"tightrein show {subject} --doc failure"
    return None
