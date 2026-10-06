"""把同一份 skills 安装到各 agent 工具(architecture/09 6.1、6.3)。

- Codex CLI 与 Antigravity CLI(agy)：在目标目录(缺省都是 ~/.agents/skills)下为每个 skill 建立指向 `skills/<名称>` 的链接，
  修改 skill 后无需重装；同一路径的链接只建一次，两个工具各自记录。
- Claude Code：构建本地插件市场(packaging/claude.py)，经 `claude plugin` 安装或更新；版本不变时不重装。
- agy 的命令白名单：为 agy 安装时在 ~/.gemini/antigravity-cli/settings.json 的 permissions.allow 中补上只读命令
  (runner/adapters/agy.READ_COMMANDS)，无人值守时 agy 据此可以用 git grep 等搜索代码；只补缺的，记进安装记录的
  allowedCommands，uninstall 只移除记录中的这些。
- 锁定的第三方 skill：缓存缺失时按锁定的 commit 下载，在 `skills/<名称>` 建立指向缓存的链接，核心的提示拼装与
  各工具都经这个位置取得。
- 先检查、后执行：`skills check` 不通过、第三方缓存与清单不符、目标位置已有不是本工具安装的条目时，列出全部问题
  后停止，不做部分安装；不覆盖他人的 skill。执行后核对每个安装位置，写 third_party/installed.json。
- uninstall 只删除 installed.json 中记录、且仍为链接的条目，以及本工具的插件。
- 只放置仓库内的第三方 skill(`install --repo-only`)：chosen 为空，只下载缓存、校验、在 `skills/<名称>` 建链接并写
  installed.json 的 repo 段，不构建插件、不执行 `claude plugin`、不碰任何工具目录；`uninstall --repo-only` 只删除 repo
  段记录的链接，仍有工具的安装记录时停止(那些工具经这些链接取得第三方 skill)。下载缓存保留。
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.config.user import UserConfig
from tightrein.domain.clock import Clock, format_iso
from tightrein.packaging import claude, skills_check, third_party
from tightrein.packaging.skills_check import CommandTree
from tightrein.packaging.third_party import Fetch, LockedSkill
from tightrein.runner.adapters import agy
from tightrein.store.files import atomic, yaml_text
from tightrein.store.files.layout import ToolLayout
from tightrein.vcs.process import Completed, VcsProcess

CLAUDE = "claude"
CODEX = "codex"
AGY = "agy"
TOOLS = (CLAUDE, CODEX, AGY)
ALL = "all"
REPO = "repo"
LINK = "link"
PLUGIN = "plugin"
HOME_PREFIX = "~/"

DOWNLOAD = "download"
CREATE_LINK = "link"
REPLACE_LINK = "relink"
REMOVE_LINK = "unlink"
BUILD = "build"
COMMAND = "command"
REMOVE_DIR = "remove"
ALLOW = "allow"
UNALLOW = "unallow"
PERMISSIONS = "permissions"

OK = "ok"
MISSING = "missing"
STALE = "stale"
CONFLICT = "conflict"


class InstallError(Exception):
    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("；".join(self.problems))


@dataclass(frozen=True)
class Target:
    tool: str
    path: Path
    method: str


@dataclass
class Context:
    tool: ToolLayout
    home: Path
    user: UserConfig
    config: ProjectConfig
    clock: Clock
    zone: tzinfo | None
    process: VcsProcess
    fetch: Fetch
    commands: CommandTree

    @property
    def agy_settings(self) -> Path:
        return self.home / agy.SETTINGS_PATH

    def cache(self, name: str, commit: str) -> Path:
        return self.tool.third_party_cache(name, commit)

    def lock(self) -> list[LockedSkill]:
        return third_party.read_lock(self.tool.third_party_lock())

    def claude(self) -> str:
        return str(self.user.tool_path(CLAUDE) or CLAUDE)

    def run(self, argv: Sequence[str]) -> Completed:
        completed = self.process.run(argv, self.tool.root)
        if completed.returncode != 0:
            raise InstallError([f"{' '.join(argv)} 失败(退出码 {completed.returncode})：{completed.stderr.strip()}"])
        return completed


@dataclass(frozen=True)
class Action:
    kind: str
    path: Path | None = None
    source: Path | None = None
    argv: tuple[str, ...] = ()
    name: str | None = None

    def describe(self) -> str:
        texts = {
            DOWNLOAD: f"下载 {self.name} 到 {self.path}",
            CREATE_LINK: f"建立链接 {self.path} → {self.source}",
            REPLACE_LINK: f"改指链接 {self.path} → {self.source}",
            REMOVE_LINK: f"删除链接 {self.path}",
            BUILD: f"构建 Claude Code 插件 {self.path}",
            COMMAND: f"执行 {' '.join(self.argv)}",
            REMOVE_DIR: f"删除目录 {self.path}",
            ALLOW: f"在 agy 白名单 {self.path} 中放行只读命令 {'、'.join(self.argv)}",
            UNALLOW: f"从 agy 白名单 {self.path} 中移除本工具放行的命令 {'、'.join(self.argv)}",
        }
        return texts[self.kind]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": str(self.path) if self.path else None,
                "source": str(self.source) if self.source else None, "argv": list(self.argv),
                "description": self.describe()}


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"actions": [action.to_dict() for action in self.actions], "problems": self.problems,
                "notes": self.notes}


@dataclass(frozen=True)
class CheckItem:
    tool: str
    name: str
    path: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"tool": self.tool, "name": self.name, "path": self.path, "status": self.status, "detail": self.detail}


# 目标与来源


def _expand(text: str, home: Path) -> Path:
    return home / text[len(HOME_PREFIX):] if text.startswith(HOME_PREFIX) else Path(text)


def targets(config: ProjectConfig, user: UserConfig, home: Path, tool: str = ALL) -> list[Target]:
    """安装目标；用户配置 install.targets.<工具>.path 覆盖 packaging.targets.<工具>，all 时跳过 enabled 为 false 的工具。"""
    found = []
    for name in TOOLS if tool == ALL else (tool,):
        setting = user.install_target(name)
        if tool == ALL and not setting.enabled:
            continue
        path = setting.path or _expand(config.get(f"packaging.targets.{name}"), home)
        found.append(Target(name, path, PLUGIN if name == CLAUDE else LINK))
    return found


def sources(ctx: Context, lock: Sequence[LockedSkill]) -> dict[str, Path]:
    """要安装的 skill → 仓库中的目录：本工具的 skill 与已锁定的第三方 skill(`skills/<名称>` 为指向缓存的链接)。"""
    third = {skill.name for skill in lock}
    skills_dir = ctx.tool.skills_dir()
    own = {path.name: path for path in sorted(skills_dir.iterdir())
           if path.name not in third and (path / skills_check.SKILL_FILE).is_file()}
    return {**own, **{skill.name: skills_dir / skill.name for skill in lock if skill.locked}}


def directory_hash(path: Path) -> str:
    return third_party.tree_hash(third_party.file_hashes(path))


# 安装记录


def read_installed(ctx: Context) -> dict[str, Any]:
    path = ctx.tool.third_party_installed()
    if not path.is_file():
        return {"tools": {}, "repo": {"links": {}}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise InstallError([f"{path} 无法解析：{error}"]) from error
    data.setdefault("tools", {})
    data.setdefault("repo", {"links": {}})
    return data


def _write_installed(ctx: Context, data: Mapping[str, Any]) -> None:
    atomic.write_text(ctx.tool.third_party_installed(), json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _recorded_links(installed: Mapping[str, Any], ctx: Context, *, excluding: Sequence[str] = ()) -> set[Path]:
    """安装记录中由本工具建立的链接；excluding 中的工具不计。"""
    found = {ctx.tool.skills_dir() / name for name in installed["repo"]["links"]}
    for tool, entry in installed["tools"].items():
        if entry.get("method") == LINK and tool not in excluding:
            found |= {Path(entry["path"]) / name for name in entry["skills"]}
    return found


# 计划


def _link(plan: Plan, destination: Path, source: Path, owned: set[Path], planned: set[Path]) -> None:
    if destination in planned:
        return
    planned.add(destination)
    if destination.is_symlink():
        if Path(os.readlink(destination)) == source:
            return
        if destination in owned:
            plan.actions.append(Action(REPLACE_LINK, destination, source))
            return
    elif not destination.exists():
        plan.actions.append(Action(CREATE_LINK, destination, source))
        return
    plan.problems.append(f"{destination} 已存在且不是本工具安装的，请处理后重试")


def _third_party(ctx: Context, plan: Plan, lock: Sequence[LockedSkill], owned: set[Path], planned: set[Path]) -> None:
    for skill in lock:
        if not skill.locked or skill.ref is None:
            plan.notes.append(f"{skill.name} 尚未锁定，未安装；先执行 tightrein third-party lock")
            continue
        cache = ctx.cache(skill.name, skill.ref)
        if cache.is_dir():
            plan.problems += [issue.describe() for issue in third_party.verify(skill, cache)]
        else:
            plan.actions.append(Action(DOWNLOAD, cache, name=skill.name))
        _link(plan, ctx.tool.skills_dir() / skill.name, cache, owned, planned)


def plan_install(ctx: Context, chosen: Sequence[Target]) -> tuple[Plan, dict[str, Any]]:
    """返回计划与安装后的记录(执行成功后写入)。"""
    plan = Plan()
    lock = ctx.lock()
    plan.problems += [f"skills check：{issue.skill} {issue.rule}：{issue.detail}" for issue in skills_check.check(
        ctx.tool.skills_dir(), ctx.commands, max_lines=int(ctx.config.get("packaging.skillBodyMaxLines")),
        toc_lines=int(ctx.config.get("packaging.referenceTocLines")), skip={skill.name for skill in lock})]
    installed = read_installed(ctx)
    owned = _recorded_links(installed, ctx)
    planned: set[Path] = set()
    _third_party(ctx, plan, lock, owned, planned)
    found = sources(ctx, lock)
    record = json.loads(json.dumps(installed))
    record["repo"]["links"] = {skill.name: str(ctx.cache(skill.name, skill.ref)) for skill in lock if skill.ref}
    now = format_iso(ctx.clock.now())
    for target in chosen:
        previous = installed["tools"].get(target.tool)
        entry: dict[str, Any] = {"path": str(target.path), "method": target.method, "installedAt": now,
                                 "skills": {name: _source_hash(ctx, source, lock) for name, source in found.items()}}
        if previous is not None and all(previous.get(key) == entry[key] for key in ("path", "method", "skills")):
            entry["installedAt"] = previous["installedAt"]
        if target.tool == AGY:
            missing = [item for item in agy.READ_COMMANDS if item not in agy.allowed(ctx.agy_settings)]
            if missing:
                plan.actions.append(Action(ALLOW, ctx.agy_settings, argv=tuple(missing)))
            entry["allowedCommands"] = sorted({*((previous or {}).get("allowedCommands") or []), *missing})
        if target.method == LINK:
            for name, source in found.items():
                _link(plan, target.path / name, source, owned, planned)
            gone = set(previous["skills"]) - set(found) if previous and previous["path"] == str(target.path) else set()
            plan.actions += [Action(REMOVE_LINK, target.path / name) for name in sorted(gone)
                             if (target.path / name).is_symlink() and target.path / name not in planned]
        else:
            entry["version"] = claude.plugin_version(entry["skills"])
            current = previous is not None and previous.get("version") == entry["version"] and target.path.is_dir()
            if previous is None and target.path.exists():
                plan.problems.append(f"{target.path} 已存在且不是本工具安装的，请处理后重试")
            elif not current:
                plan.actions.append(Action(BUILD, target.path))
                plan.actions += [Action(COMMAND, argv=tuple(argv)) for argv in
                                 claude.install_commands(ctx.claude(), target.path, previous is None)]
        record["tools"][target.tool] = entry
    return plan, record


def _source_hash(ctx: Context, source: Path, lock: Sequence[LockedSkill]) -> str:
    """第三方 skill 的链接在安装前可能还不存在，取锁定的目录哈希。"""
    for skill in lock:
        if source == ctx.tool.skills_dir() / skill.name and skill.tree_hash:
            return skill.tree_hash
    return directory_hash(source)


def plan_uninstall_repo(ctx: Context) -> tuple[Plan, dict[str, Any]]:
    """只撤销 repo 段：删除记录中仍为链接的 `skills/<名称>`。"""
    plan = Plan()
    installed = read_installed(ctx)
    if installed["tools"]:
        plan.problems.append(f"{'、'.join(sorted(installed['tools']))} 仍经 skills/<名称> 使用第三方 skill，"
                             "先执行 tightrein uninstall 卸载这些工具")
        return plan, installed
    record = json.loads(json.dumps(installed))
    for name in sorted(installed["repo"]["links"]):
        path = ctx.tool.skills_dir() / name
        if path.is_symlink():
            plan.actions.append(Action(REMOVE_LINK, path))
    record["repo"]["links"] = {}
    return plan, record


def plan_uninstall(ctx: Context, chosen: Sequence[Target]) -> tuple[Plan, dict[str, Any]]:
    plan = Plan()
    installed = read_installed(ctx)
    tools = [target.tool for target in chosen if target.tool in installed["tools"]]
    kept = _recorded_links(installed, ctx, excluding=tools)
    record = json.loads(json.dumps(installed))
    for tool in tools:
        entry = installed["tools"][tool]
        if entry.get("allowedCommands"):
            plan.actions.append(Action(UNALLOW, ctx.agy_settings, argv=tuple(entry["allowedCommands"])))
        if entry["method"] == LINK:
            for name in sorted(entry["skills"]):
                path = Path(entry["path"]) / name
                if path.is_symlink() and path not in kept and path not in {action.path for action in plan.actions}:
                    plan.actions.append(Action(REMOVE_LINK, path))
        else:
            plan.actions += [Action(COMMAND, argv=tuple(argv)) for argv in claude.uninstall_commands(ctx.claude())]
            if Path(entry["path"]).is_dir():
                plan.actions.append(Action(REMOVE_DIR, Path(entry["path"])))
        del record["tools"][tool]
    return plan, record


# 执行与核对


def _apply(ctx: Context, action: Action, lock: Mapping[str, LockedSkill], sources_: Mapping[str, Path]) -> None:
    if action.kind == DOWNLOAD and action.name is not None:
        third_party.ensure_cached(lock[action.name], ctx.fetch, ctx.cache)
    elif action.kind in (CREATE_LINK, REPLACE_LINK) and action.path is not None and action.source is not None:
        action.path.parent.mkdir(parents=True, exist_ok=True)
        if action.kind == REPLACE_LINK:
            action.path.unlink()
        action.path.symlink_to(action.source, target_is_directory=True)
    elif action.kind == REMOVE_LINK and action.path is not None:
        action.path.unlink()
    elif action.kind == BUILD and action.path is not None:
        hashes = {name: _source_hash(ctx, path, list(lock.values())) for name, path in sources_.items()}
        claude.build(action.path, sources_, claude.plugin_version(hashes))
    elif action.kind == COMMAND:
        ctx.run(action.argv)
    elif action.kind == REMOVE_DIR and action.path is not None:
        shutil.rmtree(action.path)
    elif action.kind in (ALLOW, UNALLOW) and action.path is not None:
        _edit_allow(action.path, action.argv, add=action.kind == ALLOW)


def _edit_allow(path: Path, commands: Sequence[str], *, add: bool) -> None:
    """改 agy 设置中的 permissions.allow，其余设置原样保留。"""
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    entries = [f"command({item})" for item in commands]
    if add:
        allow += [item for item in entries if item not in allow]
    else:
        allow[:] = [item for item in allow if item not in entries]
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _skill_name(directory: Path) -> str | None:
    """目录中 SKILL.md 的 frontmatter 的 name；读不到时为空。"""
    path = directory / skills_check.SKILL_FILE
    if not path.is_file():
        return None
    header, _ = skills_check.split(path.read_text(encoding="utf-8"))
    try:
        data = yaml_text.load(header) if header else None
    except yaml_text.YamlError:
        return None
    return data.get("name") if isinstance(data, Mapping) else None


def _link_items(target: Target, found: Mapping[str, Path], owned: set[Path]) -> list[CheckItem]:
    items = []
    for name, source in found.items():
        path = target.path / name
        if path.is_symlink() and Path(os.readlink(path)) == source:
            status = OK if _skill_name(path) == name else STALE
            items.append(CheckItem(target.tool, name, str(path), status, "" if status == OK else "读不到预期的 SKILL.md"))
        elif path.is_symlink() and path in owned:
            items.append(CheckItem(target.tool, name, str(path), STALE, f"链接指向 {os.readlink(path)}"))
        elif path.exists() or path.is_symlink():
            items.append(CheckItem(target.tool, name, str(path), CONFLICT, "不是本工具安装的"))
        else:
            items.append(CheckItem(target.tool, name, str(path), MISSING))
    return items


def check(ctx: Context, chosen: Sequence[Target], installed: Mapping[str, Any] | None = None) -> list[CheckItem]:
    """核对各安装位置：缺失、过期(链接指向不对，或 claude plugin list --json 列出的插件版本与当前 skills 不一致)与冲突。installed 缺省读取安装记录；
    安装刚执行完、记录还没写入时由调用方给出。已锁定的第三方 skill 在 `skills/<名称>` 的链接总是核对(工具为 repo)。"""
    lock = ctx.lock()
    installed = read_installed(ctx) if installed is None else installed
    owned = _recorded_links(installed, ctx)
    found = sources(ctx, lock)
    repo = {skill.name: ctx.cache(skill.name, skill.ref) for skill in lock if skill.locked and skill.ref}
    items = _link_items(Target(REPO, ctx.tool.skills_dir(), LINK), repo, owned)
    for target in chosen:
        if target.tool == AGY:
            missing = [item for item in agy.READ_COMMANDS if item not in agy.allowed(ctx.agy_settings)]
            items.append(CheckItem(AGY, PERMISSIONS, str(ctx.agy_settings), MISSING if missing else OK,
                                   f"白名单缺少 {'、'.join(missing)}" if missing else ""))
        if target.method == LINK:
            items += _link_items(target, found, owned)
            continue
        entry = installed["tools"].get(target.tool)
        version = claude.plugin_version({name: _source_hash(ctx, path, lock) for name, path in found.items()})
        listed = None if entry is None else claude.listed_version(
            ctx.run(claude.list_command(ctx.claude())).stdout)
        if listed is None:
            items.append(CheckItem(target.tool, claude.PLUGIN_ID, str(target.path), MISSING))
        elif listed != version:
            items.append(CheckItem(target.tool, claude.PLUGIN_ID, str(target.path), STALE,
                                   f"已安装 {listed}，当前 {version}"))
        else:
            items.append(CheckItem(target.tool, claude.PLUGIN_ID, str(target.path), OK))
    return items


def install(ctx: Context, chosen: Sequence[Target], *, dry_run: bool = False) -> Plan:
    plan, record = plan_install(ctx, chosen)
    if plan.problems:
        raise InstallError(plan.problems)
    if dry_run or not plan.actions and record == read_installed(ctx):
        return plan
    lock = {skill.name: skill for skill in ctx.lock()}
    found = sources(ctx, list(lock.values()))
    for action in plan.actions:
        _apply(ctx, action, lock, found)
    failed = [f"{item.tool} {item.name}：{item.status} {item.detail}" for item in check(ctx, chosen, record)
              if item.status != OK]
    if failed:
        raise InstallError(failed)
    _write_installed(ctx, record)
    return plan


def uninstall(ctx: Context, chosen: Sequence[Target], *, dry_run: bool = False, repo_only: bool = False) -> Plan:
    plan, record = plan_uninstall_repo(ctx) if repo_only else plan_uninstall(ctx, chosen)
    if plan.problems:
        raise InstallError(plan.problems)
    if dry_run or not plan.actions and record == read_installed(ctx):
        return plan
    for action in plan.actions:
        _apply(ctx, action, {}, {})
    _write_installed(ctx, record)
    return plan
