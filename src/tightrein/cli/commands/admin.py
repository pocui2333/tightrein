"""`tightrein admin …`：本机的安装、自检与维护。

- install [--tool claude|agy|codex] [--dry-run]：先检查后执行。检查：工具装了没有、vendor/ 的副本与 lock.json 是否一致、
  agy 的设置读不读得出；任一不通过就列出全部问题后停止，不做部分安装。执行：给 agy 的命令白名单
  (~/.gemini/antigravity-cli/settings.json 的 permissions.allow)补上只读命令表(boundaries.readCommands)中缺的，
  并在 ~/.local/bin 建 tightrein 命令链接；补了哪些、建了什么记在 ToolLayout.installed；
- uninstall [--tool …] [--dry-run]：只删记录过的：白名单中只移除当初补上的，命令链接只删仍指向本工具的；
- 不覆盖他人的同名文件：~/.local/bin/tightrein 已是别的东西时只提示；--dry-run 只列出要做什么，没有任何副作用；
- rebuild：从文件重建数据库；check：自检(配置、接入清单、凭据文件权限、工具版本、心跳失效的运行、残留的只读标记)；
- clean [--dry-run]：立即按保留期清理。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from tightrein.agents.tools import agy
from tightrein.cli import exit_codes
from tightrein.cli.assemble import Externals, ProjectUnknown, Workspace
from tightrein.cli.session import Result, Session, add_command, add_group
from tightrein.cli.text import text
from tightrein.onboard import setup as setup_file
from tightrein.protocol import vendor
from tightrein.protocol.naming import format_iso, parse_duration
from tightrein.protocol.process import Command
from tightrein.protocol.security import Redactor, child_env, load_secrets
from tightrein.settings.load import Settings
from tightrein.store import rebuild as rebuilding
from tightrein.store import retention
from tightrein.store.files.atomic import write_text
from tightrein.store.files.directories import run_dirs
from tightrein.store.files.json import read_json, write_json
from tightrein.store.tables import runs

TOOLS = ("claude", "agy", "codex")
AGY_SETTINGS = Path(".gemini") / "antigravity-cli" / "settings.json"
LOCAL_BIN = Path(".local") / "bin"
COMMAND_NAME = "tightrein"
ALLOW, UNALLOW, LINK, UNLINK = "allow", "unallow", "link", "unlink"
VERSION_TIMEOUT_S = 20.0
# 登记 store.rebuild 重建函数的模块：导入即登记(problems 在去重；issues 由评估补上后加进来)
READONLY_MARKERS = "readonly-*.json"


@dataclass(frozen=True)
class Action:
    kind: str  # allow、unallow、link、unlink
    path: Path
    commands: tuple[str, ...] = ()
    source: Path | None = None


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    record: dict[str, Any] = field(default_factory=dict)  # 执行成功后写进 ToolLayout.installed


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser,
             language: str) -> None:
    group = add_group(commands, "admin", language=language, help_key="help.admin")
    for name, handler in (("install", install), ("uninstall", uninstall)):
        parser = add_command(group, name, common=common, language=language, help_key=f"help.admin_{name}",
                             handler=handler)
        parser.add_argument("--tool", action="append", choices=TOOLS, help=text(language, "help.admin_tool"))
        parser.add_argument("--dry-run", action="store_true", help=text(language, "help.option_dry_run"))
    add_command(group, "rebuild", common=common, language=language, help_key="help.admin_rebuild", handler=rebuild)
    add_command(group, "check", common=common, language=language, help_key="help.admin_check", handler=check)
    parser = add_command(group, "clean", common=common, language=language, help_key="help.admin_clean", handler=clean)
    parser.add_argument("--dry-run", action="store_true", help=text(language, "help.option_dry_run"))


# 安装与卸载


def install(session: Session) -> Result:
    externals = session.externals
    settings = Settings.load(externals.tool)
    tools = session.args.tool or used_tools(settings)
    plan = plan_install(externals, settings, tools)
    if plan.problems:
        return _refused(session, plan)
    return _execute(session, plan)


def uninstall(session: Session) -> Result:
    externals = session.externals
    plan = plan_uninstall(externals, session.args.tool)
    return _execute(session, plan)


def used_tools(settings: Settings) -> list[str]:
    """缺省装 settings 的模型别名里用到的工具。"""
    return sorted({spec["tool"] for spec in settings.merged.get("models", {}).values() if spec.get("tool") in TOOLS})


def plan_install(externals: Externals, settings: Settings, tools: Sequence[str]) -> Plan:
    plan = Plan()
    installed = read_installed(externals)
    record = json.loads(json.dumps(installed))
    now = format_iso(externals.clock.now())
    for name in tools:
        if _executable(name, settings, externals) is None:
            plan.problems.append(f"找不到 {name}：装好后重试，或在 settings 的 tools.{name}.path 写明路径")
        entry = {"installedAt": (installed["tools"].get(name) or {}).get("installedAt") or now}
        if name == "agy":
            entry["allowedCommands"] = _plan_allow(plan, externals, settings, installed)
        record["tools"][name] = entry
    plan.problems += _vendor_problems(externals, plan)
    _plan_link(plan, externals, record)
    plan.record = record
    return plan


def plan_uninstall(externals: Externals, tools: Sequence[str] | None) -> Plan:
    plan = Plan()
    installed = read_installed(externals)
    record = json.loads(json.dumps(installed))
    chosen = [name for name in (tools or list(installed["tools"])) if name in installed["tools"]]
    for name in chosen:
        added = installed["tools"][name].get("allowedCommands") or []
        settings_path = externals.home / AGY_SETTINGS
        present = [command for command in added if command in agy.allowed(settings_path)]
        if present:
            plan.actions.append(Action(UNALLOW, settings_path, tuple(present)))
        del record["tools"][name]
    link = installed.get("commandLink")
    if not record["tools"] and link:
        path = Path(link)
        if path.is_symlink() and Path(os.readlink(path)) == externals.program:
            plan.actions.append(Action(UNLINK, path))
        record.pop("commandLink", None)
    plan.record = record
    return plan


def read_installed(externals: Externals) -> dict[str, Any]:
    path = externals.tool.installed
    data: dict[str, Any] = read_json(path) if path.is_file() else {}
    data.setdefault("tools", {})
    return data


def apply(plan: Plan) -> None:
    for action in plan.actions:
        if action.kind in (ALLOW, UNALLOW):
            _edit_allow(action.path, action.commands, add=action.kind == ALLOW)
        elif action.kind == LINK and action.source is not None:
            action.path.symlink_to(action.source)
        elif action.kind == UNLINK:
            action.path.unlink()


def _execute(session: Session, plan: Plan) -> Result:
    lines = [_describe(session, action) for action in plan.actions] + [f"  ! {note}" for note in plan.notes]
    data = {"actions": [_action_json(action) for action in plan.actions], "notes": plan.notes, "problems": []}
    if session.args.dry_run:
        return Result(session.command, lines=[session.text("cmd.admin_plan", count=len(plan.actions))] + lines,
                      data=data)
    if plan.actions and not session.confirm([_describe(session, action) for action in plan.actions]):
        return session.declined()
    apply(plan)
    write_json(session.externals.tool.installed, plan.record)
    return Result(session.command, lines=[session.text("cmd.admin_done", count=len(plan.actions))] + lines, data=data)


def _refused(session: Session, plan: Plan) -> Result:
    lines = [session.text("cmd.admin_problems", count=len(plan.problems))] + [f"  - {item}" for item in plan.problems]
    return Result(session.command, exit_codes.FAILED, lines,
                  {"actions": [], "notes": plan.notes, "problems": plan.problems})


def _plan_allow(plan: Plan, externals: Externals, settings: Settings, installed: dict[str, Any]) -> list[str]:
    """agy 的白名单只补缺的；记下的是累计补过的(卸载只删这些)。"""
    path = externals.home / AGY_SETTINGS
    if path.is_file():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            plan.problems.append(f"{path} 读不出：{type(error).__name__}")
            return []
    current = agy.allowed(path)
    missing = [command for command in settings.get("boundaries.readCommands") if command not in current]
    if missing:
        plan.actions.append(Action(ALLOW, path, tuple(missing)))
    previous = (installed["tools"].get("agy") or {}).get("allowedCommands") or []
    return sorted({*previous, *missing})


def _plan_link(plan: Plan, externals: Externals, record: dict[str, Any]) -> None:
    """~/.local/bin 存在时建 tightrein 命令链接；同名的不是本工具的链接时只提示，不覆盖。"""
    directory = externals.home / LOCAL_BIN
    link = directory / COMMAND_NAME
    if not directory.is_dir():
        plan.notes.append(f"{directory} 不存在，没有建命令链接")
        return
    if link.is_symlink() and Path(os.readlink(link)) == externals.program:
        record["commandLink"] = str(link)
    elif link.exists() or link.is_symlink():
        plan.notes.append(f"{link} 已存在且不是本工具建的，没有建命令链接")
    else:
        plan.actions.append(Action(LINK, link, source=externals.program))
        record["commandLink"] = str(link)


def _vendor_problems(externals: Externals, plan: Plan) -> list[str]:
    """vendor/ 的副本逐文件与 lock.json 核对；没锁定的只提示。"""
    lock = externals.tool.vendor_lock
    if not lock.is_file():
        return []
    problems = []
    for skill in vendor.read_lock(lock):
        if not skill.locked:
            plan.notes.append(f"vendor 中的 {skill.name} 还没有锁定，不会被加载")
            continue
        problems += [item.describe() for item in vendor.verify(skill, externals.tool.vendor_skill(skill.name))]
    return problems


def _edit_allow(path: Path, commands: Sequence[str], *, add: bool) -> None:
    """改 agy 设置中的 permissions.allow，其余设置原样保留。"""
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    entries = [f"command({command})" for command in commands]
    if add:
        allow += [item for item in entries if item not in allow]
    else:
        allow[:] = [item for item in allow if item not in entries]
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _executable(name: str, settings: Settings, externals: Externals) -> str | None:
    configured = settings.get(f"tools.{name}.path")
    if configured:
        return configured if Path(configured).is_file() else None
    return shutil.which(name, path=externals.environ.get("PATH"))


def _describe(session: Session, action: Action) -> str:
    return session.text(f"cmd.admin_{action.kind}", path=str(action.path), commands="、".join(action.commands),
                        source=str(action.source))


def _action_json(action: Action) -> dict[str, Any]:
    return {"kind": action.kind, "path": str(action.path), "commands": list(action.commands),
            "source": str(action.source) if action.source else None}


# 维护


def rebuild(session: Session) -> Result:
    if not session.confirm([session.text("cmd.rebuild_action", path=str(session.layout.database))]):
        return session.declined()
    counts = rebuilding.rebuild(session.layout, session.workspace.conn)
    lines = [session.text("cmd.rebuilt")] + [f"  {table}  {count}" for table, count in counts.items()]
    return Result(session.command, lines=lines, data=counts)


def clean(session: Session) -> Result:
    workspace, clock = session.workspace, session.externals.clock
    policy = {kind: parse_duration(value) for kind, value in workspace.settings.get("records.retention").items()}
    if session.args.dry_run:
        cutoff = clock.now() - timedelta(seconds=policy.get(retention.RUNS, 0))
        running = {run.id for run in runs.running(workspace.conn)}
        expired = [str(path) for path, started in run_dirs(workspace.layout)
                   if retention.RUNS in policy and started < cutoff and path.name not in running]
        lines = [session.text("cmd.clean_plan", count=len(expired))] + [f"  {path}" for path in expired]
        return Result(session.command, lines=lines, data={"runs": expired})
    if not session.confirm([session.text("cmd.clean_action")]):
        return session.declined()
    removed = retention.purge(workspace.layout, workspace.conn, clock, policy)
    lines = [session.text("cmd.cleaned")] + [f"  {kind}  {count}" for kind, count in removed.items()]
    return Result(session.command, lines=lines, data=removed)


def check(session: Session) -> Result:
    """自检：每项一行，有问题的退出码为 1。"""
    externals = session.externals
    items: list[dict[str, Any]] = []

    def add(name: str, problem: str | None, detail: str = "") -> None:
        items.append({"item": name, "ok": problem is None, "detail": problem or detail})

    try:
        settings = Settings.load(externals.tool)
        add("settings", None)
    except Exception as error:  # noqa: BLE001 自检：配置读不出也要接着查其余项
        add("settings", str(error))
        settings = None
    try:
        load_secrets(externals.tool.secrets, Redactor())
        add("secrets", None)
    except Exception as error:  # noqa: BLE001
        add("secrets", str(error))
    if settings is not None:
        for name in used_tools(settings):
            add(f"tool.{name}", *_tool_version(name, settings, externals))
    try:
        opened = session.workspace
    except ProjectUnknown:  # 还没有接入项目：只查本机的
        pass
    except Exception as error:  # noqa: BLE001
        add("workspace", str(error))
    else:
        _check_workspace(session, opened, add)
    problems = [item for item in items if not item["ok"]]
    lines = [session.text("cmd.check_items", count=len(items), problems=len(problems))] + [
        f"  {'ok' if item['ok'] else '!!'}  {item['item']}  {item['detail']}" for item in items]
    return Result(session.command, exit_codes.FAILED if problems else exit_codes.OK, lines, items)


def _check_workspace(session: Session, workspace: Workspace, add: Callable[..., None]) -> None:
    externals = session.externals
    layout = workspace.layout
    try:
        setup_file.load(layout)
        add("setup", None)
    except Exception as error:  # noqa: BLE001
        add("setup", str(error))
    try:
        load_secrets(layout.secrets, Redactor())
        add("workspace.secrets", None)
    except Exception as error:  # noqa: BLE001
        add("workspace.secrets", str(error))
    stale_s = workspace.settings.duration("limits.lock.stale")
    now = externals.clock.now()
    for run in runs.running(workspace.conn):
        last = run.heartbeat_at or run.started_at
        if (now - last).total_seconds() > stale_s:
            add(f"run.{run.id}", f"超过 {format_iso(last)} 没有心跳：下次运行开始时会标为中断")
    markers = sorted(layout.worktrees_dir.glob(READONLY_MARKERS)) if layout.worktrees_dir.is_dir() else []
    for marker in markers:
        add("worktree", f"残留的只读锁定标记 {marker}：下次运行开始时恢复写权限")


def _tool_version(name: str, settings: Settings, externals: Externals) -> tuple[str | None, str]:
    executable = _executable(name, settings, externals)
    if executable is None:
        return f"找不到 {name}", ""
    outcome = externals.runner.run(Command((executable, "--version"), externals.tool.root,
                                           child_env(externals.environ), timeout_s=VERSION_TIMEOUT_S))
    if outcome.exit_code != 0:
        return f"{name} --version 失败：{outcome.start_error or outcome.stopped_by or outcome.exit_code}", ""
    return None, outcome.stdout.strip().splitlines()[0] if outcome.stdout.strip() else ""
