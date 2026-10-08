"""`tightrein status`(不带命令时的缺省)：一次性快照。取数在 cli/render/snapshot.py，渲染在 cli/render/status.py。"""

from __future__ import annotations

import argparse
from dataclasses import asdict

from tightrein.cli.render.snapshot import Source, status_snapshot
from tightrein.cli.render.status import render_status
from tightrein.cli.render.style import terminal_width, use_color
from tightrein.cli.session import Result, Session, add_command


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    add_command(commands, "status", common=common, language=language, help_key="help.status", handler=handle)


def handle(session: Session) -> Result:
    snapshot = status_snapshot(source(session))
    if session.json:
        return Result(session.command, data=asdict(snapshot))
    frame = render_status(snapshot, session.language, width=terminal_width(session.stdout),
                          color=use_color(session.stdout))
    return Result(session.command, lines=[frame.rstrip("\n")])


def source(session: Session) -> Source:
    """status 与 watch 共用的取数依赖：只读 store 与工作区文件。"""
    workspace, externals = session.workspace, session.externals
    return Source(tool=externals.tool, layout=workspace.layout, conn=workspace.conn, settings=workspace.settings,
                  clock=externals.clock, host=externals.host, alive=externals.alive)
