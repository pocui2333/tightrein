"""全部路径的唯一计算处(protocol/naming.md)。其他模块只调用这里，不自己拼路径。

两类根目录：
- ToolLayout：tightrein 仓库(全局 settings/、vendor/、workspaces/)；
- WorkspaceLayout：一个项目的工作区 `workspaces/<项目>/`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tightrein.protocol.naming import FileName, segment

TOOL_ROOT_ENV = "TIGHTREIN_HOME"


@dataclass(frozen=True)
class ToolLayout:
    root: Path

    @classmethod
    def discover(cls) -> ToolLayout:
        """`TIGHTREIN_HOME` 优先；否则取包所在仓库(src 布局下为 src/ 的上一层)。"""
        configured = os.environ.get(TOOL_ROOT_ENV)
        if configured:
            return cls(Path(configured).expanduser().resolve())
        return cls(Path(__file__).resolve().parents[4])

    @property
    def settings_dir(self) -> Path:
        return self.root / "settings"

    @property
    def defaults(self) -> Path:
        return self.settings_dir / "defaults.json"

    @property
    def controls(self) -> Path:
        return self.settings_dir / "controls.json"

    @property
    def sites(self) -> Path:
        return self.settings_dir / "sites.json"

    @property
    def secrets(self) -> Path:
        return self.settings_dir / "secrets.json"

    @property
    def vendor_dir(self) -> Path:
        return self.root / "vendor"

    @property
    def vendor_lock(self) -> Path:
        return self.vendor_dir / "lock.json"

    def vendor_skill(self, name: str) -> Path:
        return self.vendor_dir / "skills" / segment(name)

    @property
    def workspaces_dir(self) -> Path:
        return self.root / "workspaces"

    def workspace(self, project: str) -> WorkspaceLayout:
        return WorkspaceLayout(self.workspaces_dir / segment(project))

    @property
    def state_dir(self) -> Path:
        """本机状态(不属于任何项目)：tightrein 装进了哪些工具等。"""
        return self.root / "local"

    @property
    def installed(self) -> Path:
        return self.state_dir / "installed.json"


@dataclass(frozen=True)
class WorkspaceLayout:
    root: Path

    @property
    def project(self) -> str:
        return self.root.name

    # 接入与配置
    @property
    def setup(self) -> Path:
        return self.root / "setup.json"

    @property
    def setup_md(self) -> Path:
        return self.root / "setup.md"

    @property
    def settings(self) -> Path:
        return self.root / "settings.json"

    @property
    def sites(self) -> Path:
        return self.root / "sites.json"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets.json"

    @property
    def scripts_dir(self) -> Path:
        return self.root / "scripts"

    # 知识库
    @property
    def knowledge_dir(self) -> Path:
        return self.root / "knowledge"

    def knowledge_kind(self, kind: str) -> Path:
        return self.knowledge_dir / segment(kind)

    @property
    def knowledge_proposals(self) -> Path:
        """待用户确认的「建议沉淀」。"""
        return self.knowledge_dir / "proposals.json"

    @property
    def knowledge_cache(self) -> Path:
        """上一次读好的知识条目：有条目写坏时照常基于它读取。"""
        return self.cache_dir / "knowledge-entries.json"

    # 运行数据
    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def database(self) -> Path:
        return self.data_dir / "tightrein.db"

    @property
    def run_lock(self) -> Path:
        return self.data_dir / "run.lock"

    @property
    def control_file(self) -> Path:
        """运行控制状态(暂停、急停)，pause、stop、resume 写它。"""
        return self.data_dir / "control.json"

    def object_lock(self, subject: str) -> Path:
        return self.data_dir / "locks" / f"{segment(subject)}.lock"

    @property
    def issues_dir(self) -> Path:
        return self.data_dir / "issues"

    def issue_dir(self, issue: str) -> Path:
        return self.issues_dir / segment(issue)

    @property
    def problems_dir(self) -> Path:
        return self.data_dir / "problems"

    def problem_dir(self, problem: str) -> Path:
        return self.problems_dir / segment(problem)

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    def run_dir(self, run: str) -> Path:
        return self.runs_dir / segment(run)

    def events(self, run: str) -> Path:
        return self.run_dir(run) / "events.jsonl"

    @property
    def retro_dir(self) -> Path:
        return self.data_dir / "retro"

    @property
    def cache_dir(self) -> Path:
        """可随时删除的缓存：基准检查、接口描述等按 commit 或哈希缓存的结果。"""
        return self.data_dir / "cache"

    @property
    def worktrees_dir(self) -> Path:
        return self.root / "worktrees"

    def worktree(self, name: str) -> Path:
        return self.worktrees_dir / segment(name)

    # 对象目录中的文件
    def subject_dir(self, subject: str) -> Path:
        """问题、Issue 或运行的目录，按编号的种类分派。"""
        if subject.startswith("R-"):
            return self.run_dir(subject)
        if subject.startswith("P-"):
            return self.problem_dir(subject)
        return self.issue_dir(subject)

    def attempt_dir(self, subject: str, number: int) -> Path:
        """Issue 上一次修复尝试的归档(回归、重开后重新修复时，把那一次实施与发布的文件挪进来)：`attempt_1/`。"""
        if number < 1:
            raise ValueError(f"尝试的编号从 1 起：{number}")
        return self.subject_dir(subject) / f"attempt_{number}"

    def step_file(self, subject: str, name: FileName) -> Path:
        return self.subject_dir(subject) / name.render()

    def shared_file(self, subject: str, content: str, extension: str) -> Path:
        """对象共用的文件(序号 00)：`00-issue-notes.json`、`00-issue-body.md`。"""
        kind = "run" if subject.startswith("R-") else "problem" if subject.startswith("P-") else "issue"
        return self.subject_dir(subject) / f"00-{kind}-{segment(content)}.{extension}"

    def human_document(self, subject: str, content: str) -> Path:
        """给人看的三种文档(序号 90)：`90-issue-pending.md` 等。"""
        if content not in ("pending", "failure", "deliver"):
            raise ValueError(f"不是给人看的文档：{content}")
        kind = "problem" if subject.startswith("P-") else "issue"
        return self.subject_dir(subject) / f"90-{kind}-{content}.md"
