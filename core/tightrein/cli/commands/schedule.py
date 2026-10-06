"""schedule install|uninstall|show(architecture/09 8.1)：生成、加载、卸载与查看工作区的 launchd 定时任务。

install 与 uninstall 展示 plist 全文与将执行的 launchctl 命令，终端中输入 yes 后才写入或删除；--dry-run 只展示；
非交互调用停在关口。时间表取 schedule.tick(project.yaml 覆盖核心缺省值)。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import confirmed, group, leaf, not_confirmed
from tightrein.cli.output import Outcome
from tightrein.packaging import launchd
from tightrein.store.files.layout import UserLayout
from tightrein.vcs.process import Completed, VcsProcess


def _launchd(app: Any) -> launchd.Launchd:
    process = VcsProcess(execute=app.externals.vcs_execute, environ=dict(app.environ))

    def run(argv: Any) -> Completed:
        return process.run(argv, app.tool.root)

    return launchd.Launchd(run, os.getuid())


def _plist_path(app: Any) -> Path:
    return UserLayout(app.home).launch_agent(app.layout.project)


def _install(invocation: Any) -> Outcome:
    app = invocation.app
    program = app.externals.program
    if not program.is_file():
        raise exit_codes.UsageError(f"找不到 {program}：先在虚拟环境中重新安装核心包(pip install -e core)")
    path = _plist_path(app)
    content = launchd.render(launchd.build(
        path, app.layout, program, app.tool.root, app.config.get("schedule.tick"),
        app.externals.environ.get("PATH", ""), app.config.get("schedule.launchd.lang")))
    agent = _launchd(app)
    commands = [" ".join(argv) for argv in agent.install_commands(path)]
    lines = [f"写入 {path}：", content.decode("utf-8"), "将执行：", *commands]
    result = {"path": str(path), "plist": content.decode("utf-8"), "commands": commands}
    if invocation.args.dry_run:
        return Outcome("schedule install", exit_codes.OK, lines, result=result)
    if not confirmed(invocation, "\n".join(lines)):
        if not invocation.interactive:
            return not_confirmed("schedule install", lines, result)
        return Outcome("schedule install", exit_codes.OK, ["已取消"], result=result)
    agent.install(path, content)
    return Outcome("schedule install", exit_codes.OK, [f"已加载 {launchd.label(path)}", *commands], result=result)


def _uninstall(invocation: Any) -> Outcome:
    app = invocation.app
    path = _plist_path(app)
    if not path.exists():
        return Outcome("schedule uninstall", exit_codes.OK, [f"没有 {path}"], result={"path": str(path)})
    agent = _launchd(app)
    commands = [" ".join(argv) for argv in agent.uninstall_commands(path)]
    lines = ["将执行：", *commands, f"然后删除 {path}"]
    result = {"path": str(path), "commands": commands}
    if invocation.args.dry_run:
        return Outcome("schedule uninstall", exit_codes.OK, lines, result=result)
    if not confirmed(invocation, "\n".join(lines)):
        if not invocation.interactive:
            return not_confirmed("schedule uninstall", lines, result)
        return Outcome("schedule uninstall", exit_codes.OK, ["已取消"], result=result)
    agent.uninstall(path)
    return Outcome("schedule uninstall", exit_codes.OK, [f"已卸载 {launchd.label(path)}"], result=result)


def _show(invocation: Any) -> Outcome:
    app = invocation.app
    path = _plist_path(app)
    if not path.exists():
        return Outcome("schedule show", exit_codes.OK, [f"没有安装定时任务：{path}"],
                       result={"path": str(path), "plist": None, "status": None},
                       next="tightrein project schedule install")
    plist = path.read_text(encoding="utf-8")
    status = _launchd(app).show(path)
    return Outcome("schedule show", exit_codes.OK, [str(path), plist, status],
                   result={"path": str(path), "plist": plist, "status": status})


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    schedule = group(commands, "schedule", "launchd 定时任务")
    leaf(schedule, common, "install", _install, "生成并加载定时任务")
    leaf(schedule, common, "uninstall", _uninstall, "卸载并删除定时任务")
    leaf(schedule, common, "show", _show, "查看定时任务的配置与状态")
