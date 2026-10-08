"""`tightrein retro …`：tightrein 自身问题的记录簿(retro/README.md)。

- list [--rating P0..P3] [--all]：按评级列出待看的记录(--all 含已处理与不处理的)；
- show <编号>：详情与每次出现的细节(记录文件原文)；
- close <编号> --done | --wontfix：处理完或不处理时关闭。
"""

from __future__ import annotations

import argparse

from tightrein.cli.session import Result, Session, add_command, add_group
from tightrein.cli.text import text
from tightrein.retro import records
from tightrein.retro.records import RATINGS, RecordStatus


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    group = add_group(commands, "retro", language=language, help_key="help.retro")
    parser = add_command(group, "list", common=common, language=language, help_key="help.retro_list", handler=list_)
    parser.add_argument("--rating", choices=RATINGS)
    parser.add_argument("--all", action="store_true", help=text(language, "help.retro_all"))
    parser = add_command(group, "show", common=common, language=language, help_key="help.retro_show", handler=show)
    parser.add_argument("number", metavar="<id>", help=text(language, "help.arg_retro"))
    parser = add_command(group, "close", common=common, language=language, help_key="help.retro_close",
                         handler=close)
    parser.add_argument("number", metavar="<id>", help=text(language, "help.arg_retro"))
    verdict = parser.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--done", dest="status", action="store_const", const=RecordStatus.DONE,
                         help=text(language, "help.retro_done"))
    verdict.add_argument("--wontfix", dest="status", action="store_const", const=RecordStatus.WONTFIX,
                         help=text(language, "help.retro_wontfix"))


def list_(session: Session) -> Result:
    statuses = tuple(RecordStatus) if session.args.all else (RecordStatus.OPEN,)
    found = [record for record in records.listing(session.layout, statuses)
             if session.args.rating is None or record.rating == session.args.rating]
    items = [{"id": record.id, "rating": record.rating, "status": record.status.value, "kind": record.kind.value,
              "point": record.point, "count": record.count, "lastSeen": record.last_seen, "fact": record.fact}
             for record in found]
    lines = [session.text("cmd.retro_count", count=len(items))] + [
        f"  {item['id']}  {item['rating']}  ×{item['count']}  {item['point']}  {item['fact']}" for item in items]
    return Result(session.command, lines=lines, data=items)


def show(session: Session) -> Result:
    record = records.get(session.layout, session.args.number)
    return Result(session.command, lines=[record.render().rstrip("\n")], data=record.to_json())


def close(session: Session) -> Result:
    record = records.get(session.layout, session.args.number)
    status: RecordStatus = session.args.status
    if not session.confirm([session.text("cmd.retro_close_action", subject=record.id, status=status.value)]):
        return session.declined()
    updated = records.set_status(session.layout, record.id, status)
    return Result(session.command, lines=[session.text("cmd.retro_closed", subject=updated.id, status=status.value)],
                  data=updated.to_json())
