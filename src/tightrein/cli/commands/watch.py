"""`tightrein watch [--collect]`：实时界面。只读数据库与文件，不调用模型、不写记录，随时可开可关；
界面里按 [p] 暂停、[s] 急停由这里写运行控制(与 tightrein pause、stop 相同)。--json 时输出一次快照。"""

from __future__ import annotations

import argparse
from dataclasses import asdict

from tightrein.cli.commands import control
from tightrein.cli.commands.status import source
from tightrein.cli.render import live
from tightrein.cli.render.snapshot import watch_snapshot
from tightrein.cli.session import Result, Session, add_command
from tightrein.cli.text import text


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    parser = add_command(commands, "watch", common=common, language=language, help_key="help.watch", handler=handle)
    parser.add_argument("--collect", action="store_true", help=text(language, "help.watch_collect"))


def handle(session: Session) -> Result:
    fetched = source(session)
    if session.json:
        return Result(session.command, data=asdict(watch_snapshot(fetched)))

    def on_key(key: str) -> None:
        if key == live.PAUSE:
            control.pause_now(session, note="watch")
        elif key == live.STOP:
            control.stop_now(session, note="watch")

    live.run(lambda: watch_snapshot(fetched), session.language, collect_view=session.args.collect, on_key=on_key,
             stdin=session.stdin, stdout=session.stdout)
    return Result(session.command)
