"""`tightrein knowledge …`：项目的知识库(knowledge/README.md)。

- list [--pending | --stale]：条目；--pending 列出各步骤提出、待确认的「建议沉淀」，--stale 列出涉及的文件变了、待确认的条目；
- show <编号>：条目(CON-0001 等)或建议(0003)的原文；
- confirm <建议编号>：确认一条建议，由模型对照已有条目写成完整条目(knowledge.propose.accept，会调用模型)；
- drop <建议编号> [--note]：放弃一条建议；
- add --kind … --slug … --title … --summary … --body … [--location …]：手写一条。
"""

from __future__ import annotations

import argparse
from typing import Any

from tightrein.cli.session import Result, Session, add_command, add_group
from tightrein.cli.text import text
from tightrein.knowledge import entries, propose
from tightrein.knowledge.entries import KINDS, EntryStatus
from tightrein.protocol.naming import format_iso, local_date

ENTRY_ID_SEPARATOR = "-"  # 条目编号带前缀(CON-0001)，建议编号是纯数字


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    group = add_group(commands, "knowledge", language=language, help_key="help.knowledge")
    parser = add_command(group, "list", common=common, language=language, help_key="help.knowledge_list",
                         handler=list_)
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--pending", action="store_true", help=text(language, "help.knowledge_pending"))
    which.add_argument("--stale", action="store_true", help=text(language, "help.knowledge_stale"))
    parser = add_command(group, "show", common=common, language=language, help_key="help.knowledge_show",
                         handler=show)
    parser.add_argument("number", metavar="<id>", help=text(language, "help.arg_knowledge"))
    parser = add_command(group, "confirm", common=common, language=language, help_key="help.knowledge_confirm",
                         handler=confirm)
    parser.add_argument("number", metavar="<id>", help=text(language, "help.arg_knowledge_proposal"))
    parser = add_command(group, "drop", common=common, language=language, help_key="help.knowledge_drop",
                         handler=drop)
    parser.add_argument("number", metavar="<id>", help=text(language, "help.arg_knowledge_proposal"))
    parser.add_argument("--note", help=text(language, "help.option_note"))
    parser = add_command(group, "add", common=common, language=language, help_key="help.knowledge_add", handler=add)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--slug", required=True, help=text(language, "help.knowledge_slug"))
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--location", action="append", default=[], help=text(language, "help.knowledge_location"))


def list_(session: Session) -> Result:
    if session.args.pending:
        found = propose.pending(session.layout)
        lines = [session.text("cmd.knowledge_pending", count=len(found))] + [
            f"  {item.id}  {', '.join(item.subjects)}  {item.text}" for item in found]
        return Result(session.command, lines=lines, data=[item.to_json() for item in found],
                      next=f"tightrein knowledge confirm {found[0].id}" if found else None)
    wanted = EntryStatus.STALE if session.args.stale else EntryStatus.ACTIVE
    loaded = entries.load(session.layout)
    chosen = [entry for entry in loaded.entries if entry.status is wanted]
    lines = [session.text("cmd.knowledge_count", count=len(chosen))] + [
        f"  {entry.id}  {entry.title}" + (f"  ({entry.stale_reason})" if entry.stale_reason else "") for entry in chosen]
    lines += [f"  ! {warning}" for warning in loaded.warnings]
    return Result(session.command, lines=lines, data=[_entry(entry) for entry in chosen])


def show(session: Session) -> Result:
    number = session.args.number
    if ENTRY_ID_SEPARATOR in number:
        entry = entries.find(entries.load(session.layout).entries, number)
        if entry is None:
            raise LookupError(session.text("cmd.no_subject", subject=number))
        return Result(session.command, lines=[entry.render().rstrip("\n")], data=_entry(entry))
    proposal = next((item for item in propose.read(session.layout) if item.id == number.zfill(4)), None)
    if proposal is None:
        raise LookupError(session.text("cmd.no_subject", subject=number))
    lines = [f"{proposal.id}  {proposal.status.value}  {', '.join(proposal.subjects)}", proposal.text]
    return Result(session.command, lines=lines, data=proposal.to_json())


def confirm(session: Session) -> Result:
    number = session.args.number.zfill(4)
    if not session.confirm([session.text("cmd.knowledge_confirm_action", subject=number)]):
        return session.declined()
    current = session.begin("run")
    accepted = propose.accept(current, number)
    session.end(current)
    return Result(session.command, lines=[session.text("cmd.knowledge_confirmed", subject=number,
                                                       entry=accepted.entry or "-")],
                  data=accepted.to_json(), next=f"tightrein knowledge show {accepted.entry}" if accepted.entry else None)


def drop(session: Session) -> Result:
    number = session.args.number.zfill(4)
    if not session.confirm([session.text("cmd.knowledge_drop_action", subject=number)]):
        return session.declined()
    reason = session.args.note or session.text("cmd.knowledge_dropped_by_user")
    dropped = propose.reject(session.layout, number, reason, format_iso(session.externals.clock.now()))
    return Result(session.command, lines=[session.text("cmd.knowledge_dropped", subject=number)],
                  data=dropped.to_json())


def add(session: Session) -> Result:
    args = session.args
    if not session.confirm([session.text("cmd.knowledge_add_action", kind=args.kind, title=args.title)]):
        return session.declined()
    commit = session.runtime("run").git.head().commit  # 过期比对的基准
    entry = entries.create(session.layout, kind=args.kind, slug=args.slug, title=args.title, summary=args.summary,
                           body=args.body, locations=tuple(args.location),
                           today=local_date(session.externals.clock.now()).isoformat(), commit=commit, sources=())
    return Result(session.command, lines=[session.text("cmd.knowledge_added", subject=entry.id)], data=_entry(entry),
                  next=f"tightrein knowledge show {entry.id}")


def _entry(entry: entries.Entry) -> dict[str, Any]:
    return entry.to_json() | {"title": entry.title, "path": str(entry.path) if entry.path else None}
