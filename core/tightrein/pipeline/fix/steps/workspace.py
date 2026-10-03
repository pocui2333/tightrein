"""修复分支名与 worktree 准备(architecture/07 3.3，redesign/07-release.md 第 1 节)。

分支名按项目约定(pipeline/common/conventions.py)生成，通用格式为 `<类型>/<Issue 编号>-<简称>`，类型由任务类型
映射(git.branchTypes，立即修的 P0 为 hotfix)，简称取自 Issue 文件名；项目要求个人前缀时在前面加本机用户配置的
branchPrefix，前缀不得是 git.forbiddenPrefixes 中的 AI 与工具名称(不区分大小写)。分支名已存在且不属于本 Issue 时加
`-2` 后缀。
"""

from __future__ import annotations

from collections.abc import Callable

from tightrein.config.project import ProjectConfig
from tightrein.domain import release_format
from tightrein.domain.issue import Issue
from tightrein.pipeline.checks import project_checks
from tightrein.pipeline.checks.project_checks import CheckCommand
from tightrein.pipeline.common.conventions import Conventions

PREPARE_KEY = "checks.prepare"
SUFFIX = 2


class BranchRejected(ValueError):
    """项目要求个人前缀而没有配置，或前缀使用了禁用名称。"""


def forbidden(prefix: str, config: ProjectConfig) -> bool:
    return prefix.lower() in {item.lower() for item in config.get("git.forbiddenPrefixes")}


def branch_name(issue: Issue, prefix: str | None, conventions: Conventions, config: ProjectConfig, *,
                exists: Callable[[str], bool], current: str | None = None) -> str:
    if conventions.personal_prefix:
        if not prefix:
            raise BranchRejected("本项目的分支名要求个人前缀，先在 ~/.config/tightrein/config.yaml 设置 branchPrefix")
        if forbidden(prefix, config):
            raise BranchRejected(f"分支前缀不能使用 AI 或工具名称：{prefix}")
    kind = release_format.branch_type(issue.task_type, issue.treatment, issue.severity, config.get("git.branchTypes"))
    base = release_format.branch(conventions.branch, kind=kind, issue_id=issue.id, slug=issue.slug,
                                 prefix=prefix if conventions.personal_prefix else None)
    if base != current and exists(base):
        return f"{base}-{SUFFIX}"
    return base


def prepare_commands(config: ProjectConfig) -> list[CheckCommand]:
    return project_checks.commands(config, PREPARE_KEY)
