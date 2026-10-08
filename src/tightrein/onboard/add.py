"""建工作区(接入第 1、2 步，`tightrein project add <仓库路径>`)：建目录、探测仓库、写草稿与 setup.md。

项目专属的一切都在工作区里(44 号计划「工作区目录」)；仓库本身不动。已有同名工作区时拒绝，不覆盖用户填过的内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tightrein.onboard import render
from tightrein.onboard.detect import Detection, convention_lines, detect, hints, settings_draft, setup_draft
from tightrein.onboard.setup import issues_of
from tightrein.protocol.git import Git
from tightrein.protocol.naming import Clock, format_iso, segment
from tightrein.store.db import open_database
from tightrein.store.files.json import write_json
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

KNOWLEDGE_KINDS = ("conventions", "patterns", "lessons")
SECRETS_MODE = 0o600


@dataclass(frozen=True)
class Plan:
    workspace: WorkspaceLayout
    repo: Path
    directories: list[Path]
    files: list[Path]

    @property
    def creates(self) -> list[Path]:
        """要建的目录与文件，按先后。"""
        return self.directories + self.files


@dataclass(frozen=True)
class Added:
    workspace: WorkspaceLayout
    detection: Detection
    issues: list[str]  # 草稿里还要用户定的(setup.load 报出的问题)


def plan(tool: ToolLayout, repo: Path, name: str | None = None) -> Plan:
    """先列出要做什么(给用户确认)；仓库不是 git 仓库、同名工作区已存在时报错。"""
    repo = repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"不是 git 仓库：{repo}")
    workspace = tool.workspace(segment(name or repo.name))
    if workspace.root.exists():
        raise ValueError(f"工作区已存在：{workspace.root}(换一个 --name，或先 tightrein project remove)")
    directories = [workspace.root, workspace.scripts_dir, *(workspace.knowledge_kind(kind) for kind in KNOWLEDGE_KINDS),
                   workspace.data_dir, workspace.worktrees_dir]
    files = [workspace.settings, workspace.setup, workspace.sites, workspace.secrets, workspace.database,
             workspace.setup_md]
    return Plan(workspace, repo, directories, files)


def add(plan: Plan, *, git: Git, clock: Clock, language: str) -> Added:
    """按 plan 建工作区；git 为仓库上的只读 Git(探测主分支与远程)。"""
    workspace = plan.workspace
    detection = detect(plan.repo, git)
    for directory in plan.directories:
        directory.mkdir(parents=True, exist_ok=True)
    setup = setup_draft(detection, workspace.project, format_iso(clock.now()))
    settings = settings_draft(detection, language)
    write_json(workspace.settings, settings)
    write_json(workspace.setup, setup)
    write_json(workspace.sites, {})
    write_json(workspace.secrets, {}, mode=SECRETS_MODE)
    open_database(workspace.database, clock=clock).close()
    issues = issues_of(setup, workspace)
    page = render.Page(project=workspace.project, setup=setup, facts=settings["project"], issues=issues, trials=(),
                       checked_at=None, hints=hints(detection), conventions=convention_lines(detection.conventions))
    render.write(workspace.setup_md, page, language)
    return Added(workspace, detection, issues)
