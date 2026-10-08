"""`tightrein project …`：接入项目(onboard/README.md「接入步骤」)。

- add <仓库路径> [--name]：建工作区并探测，写 setup.json、settings.json 草稿与 setup.md；
- check [<模块>]：试跑启用与自定义的模块、主分支上的检查命令；结果写进 setup.md；
- ready：试跑全部通过且之后没改过 setup.json 才标为就绪，并装上定时器(macOS 为 launchd)；
- show：看接入清单(setup.md 的内容)；config [<键>] [--explain]：看取值及来自哪一层；
- list：列出所有项目；remove <名>：卸下定时器、删除工作区(不动仓库)。
"""

from __future__ import annotations

import argparse
import json
import shutil
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.assemble import projects
from tightrein.cli.session import Result, Session, add_command, add_group, pick_language
from tightrein.cli.text import text
from tightrein.onboard import add as onboarding
from tightrein.onboard import check as trial
from tightrein.onboard import render
from tightrein.onboard import setup as setup_file
from tightrein.onboard.detect import convention_lines
from tightrein.onboard.setup import SetupInvalid
from tightrein.protocol.git import Git
from tightrein.protocol.naming import format_iso
from tightrein.protocol.schedule import launchd
from tightrein.protocol.security import child_env
from tightrein.settings.load import Settings
from tightrein.store.files.json import read_json
from tightrein.store.tables import state

MACOS = "darwin"
DEFAULT_LANG_ENV = "en_US.UTF-8"


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    group = add_group(commands, "project", language=language, help_key="help.project")
    parser = add_command(group, "add", common=common, language=language, help_key="help.project_add", handler=add)
    parser.add_argument("repo", metavar="<path>", help=text(language, "help.arg_repo"))
    parser.add_argument("--name", help=text(language, "help.project_name"))
    parser = add_command(group, "check", common=common, language=language, help_key="help.project_check",
                         handler=check)
    parser.add_argument("module", nargs="?", metavar="<module>", help=text(language, "help.arg_module"))
    add_command(group, "ready", common=common, language=language, help_key="help.project_ready", handler=ready)
    add_command(group, "show", common=common, language=language, help_key="help.project_show", handler=show)
    parser = add_command(group, "config", common=common, language=language, help_key="help.project_config",
                         handler=config)
    parser.add_argument("key", nargs="?", metavar="<key>", help=text(language, "help.arg_key"))
    parser.add_argument("--explain", action="store_true", help=text(language, "help.project_explain"))
    add_command(group, "list", common=common, language=language, help_key="help.project_list", handler=list_)
    parser = add_command(group, "remove", common=common, language=language, help_key="help.project_remove",
                         handler=remove)
    parser.add_argument("name", metavar="<name>", help=text(language, "help.arg_workspace"))


def add(session: Session) -> Result:
    externals = session.externals
    plan = onboarding.plan(externals.tool, externals.cwd / session.args.repo, session.args.name)
    if not session.confirm([session.text("cmd.create", path=str(path)) for path in plan.creates]):
        return session.declined()
    settings = Settings.load(externals.tool)
    git = Git(plan.repo, externals.runner, child_env(externals.environ), settings)
    added = onboarding.add(plan, git=git, clock=externals.clock, language=pick_language(session.args.lang or "zh"))
    project = added.workspace.project
    lines = [session.text("cmd.project_added", project=project, path=str(added.workspace.root)),
             session.text("cmd.project_to_fill", count=len(added.issues), path=str(added.workspace.setup_md))]
    lines += [f"  - {item}" for item in added.issues]
    lines += [f"  * {item}" for item in convention_lines(added.detection.conventions)]
    data = {"project": project, "workspace": str(added.workspace.root), "issues": added.issues,
            "detected": {"mainBranch": added.detection.main_branch, "commands": added.detection.commands,
                         "stacks": added.detection.stacks, "spec": added.detection.spec,
                         "sentry": added.detection.sentry, "ci": added.detection.ci,
                         "conventions": convention_lines(added.detection.conventions)}}
    return Result(session.command, lines=lines, data=data, next=f"tightrein project check -p {project}")


def check(session: Session) -> Result:
    layout, conn = session.layout, session.workspace.conn
    previous = trial.last_report(conn)
    try:
        setup_file.load(layout)
    except SetupInvalid as invalid:
        render.refresh(layout, trials=previous.trials if previous else (), checked_at=previous.at if previous else None,
                       language=session.language)
        lines = [session.text("cmd.setup_invalid", path=str(layout.setup))] + [f"  - {item}" for item in invalid.issues]
        return Result(session.command, exit_codes.FAILED, lines, {"issues": invalid.issues})
    report = trial.check(session.runtime("run"), only=session.args.module)
    render.refresh(layout, trials=report.trials, checked_at=report.at, language=session.language)
    counts = trial.summary(report.trials)
    lines = [session.text("cmd.check_summary", **counts, path=str(layout.setup_md))]
    lines += [f"  {item.key}  {session.text('cmd.trial_' + item.status)}  {item.detail}" for item in report.trials]
    lines += [f"      {error}" for item in report.trials if item.status == trial.FAILED for error in item.errors]
    return Result(session.command, exit_codes.OK if report.passed else exit_codes.FAILED, lines, report.to_json(),
                  "tightrein project ready" if report.passed else None)


def ready(session: Session) -> Result:
    layout, conn, externals = session.layout, session.workspace.conn, session.externals
    setup_file.load(layout)
    problems = trial.ready_problems(trial.last_report(conn), trial.setup_hash(layout.setup))
    if problems:
        lines = [session.text("cmd.not_ready")] + [f"  - {item}" for item in problems]
        return Result(session.command, exit_codes.FAILED, lines, {"problems": problems}, "tightrein project check")
    job = _job(session)
    actions = [session.text("cmd.mark_ready", project=layout.project)]
    if job is not None:
        actions.append(session.text("cmd.install_timer", path=str(job.plist), tick=session.workspace.settings.get(
            "schedule.tick")))
    if not session.confirm(actions):
        return session.declined()
    state.put(conn, trial.READY_KEY, {"at": format_iso(externals.clock.now())}, externals.clock)
    lines = [session.text("cmd.ready", project=layout.project)]
    if job is not None:
        launchd.Launchd(externals.runner, externals.uid, externals.tool.root).install(job)
        lines.append(session.text("cmd.timer_installed", path=str(job.plist)))
    else:
        lines.append(session.text("cmd.no_timer", platform=externals.platform))
    return Result(session.command, lines=lines, data={"ready": True, "plist": str(job.plist) if job else None},
                  next="tightrein status")


def show(session: Session) -> Result:
    layout = session.layout
    report = trial.last_report(session.workspace.conn)
    path = render.refresh(layout, trials=report.trials if report else (), checked_at=report.at if report else None,
                          language=session.language)
    data: dict[str, Any] = {"setup": read_json(layout.setup) if layout.setup.is_file() else None,
                            "check": report.to_json() if report else None,
                            "ready": bool(state.get(session.workspace.conn, trial.READY_KEY))}
    return Result(session.command, lines=[path.read_text(encoding="utf-8").rstrip("\n")], data=data)


def config(session: Session) -> Result:
    settings = session.workspace.settings
    key = session.args.key
    if key is None:
        return Result(session.command, lines=[_dump(settings.merged)], data=settings.merged)
    if session.args.explain:
        layers = [{"layer": layer, "value": value} for layer, value in settings.explain(key)]
        lines = [key] + [f"  {item['layer']}: {_dump(item['value'])}" for item in layers]
        return Result(session.command, lines=lines, data={"key": key, "layers": layers})
    value = _value(settings, key)
    return Result(session.command, lines=[f"{key} = {_dump(value)}"], data={"key": key, "value": value})


def list_(session: Session) -> Result:
    tool = session.externals.tool
    items = []
    for name in projects(tool):
        workspace = tool.workspace(name)
        repo = (read_json(workspace.settings).get("project") or {}).get("repo")
        items.append({"project": name, "repo": repo, "workspace": str(workspace.root)})
    lines = [session.text("cmd.project_count", count=len(items))] + [
        f"  {item['project']}  {item['repo'] or '-'}" for item in items]
    return Result(session.command, lines=lines, data=items)


def remove(session: Session) -> Result:
    externals = session.externals
    name = session.args.name
    if name not in projects(externals.tool):
        raise LookupError(session.text("cmd.no_project", project=name))
    workspace = externals.tool.workspace(name)
    plist = launchd.plist_path(externals.home, name)
    actions = ([session.text("cmd.uninstall_timer", path=str(plist))] if plist.exists() else []) + [
        session.text("cmd.delete", path=str(workspace.root))]
    if not session.confirm(actions):
        return session.declined()
    if plist.exists():
        launchd.Launchd(externals.runner, externals.uid, externals.tool.root).uninstall(launchd.label(name), plist)
    shutil.rmtree(workspace.root)
    return Result(session.command, lines=[session.text("cmd.project_removed", project=name)],
                  data={"project": name, "removed": str(workspace.root)})


def _job(session: Session) -> launchd.Job | None:
    externals, settings = session.externals, session.workspace.settings
    if externals.platform != MACOS:
        return None
    return launchd.build(workspace=session.layout, home=externals.home, program=externals.program,
                         tool_root=externals.tool.root, interval_s=settings.duration("schedule.tick"),
                         path_env=externals.environ.get("PATH", ""),
                         lang=externals.environ.get("LANG") or DEFAULT_LANG_ENV)


def _value(settings: Settings, key: str) -> Any:
    """控制键本身带点：controls.implement.code.turns 取 implement.code 的 turns(按继承取生效值)。"""
    parts = key.split(".")
    if parts[0] == "controls" and len(parts) > 2:
        return settings.control(".".join(parts[1:-1]), parts[-1])
    return settings.get(key)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else json.dumps(
        value, ensure_ascii=False)
