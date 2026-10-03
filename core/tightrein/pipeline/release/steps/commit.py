"""提交信息与文件清单(architecture/07 19.1 第 2、3 步，redesign/07-release.md 第 1 节)。

提交信息按项目约定的格式(通用为 Conventional Commits)：类型由任务类型映射(git.commitTypes)，范围、一句话与原因取
fix-executor 给出的 release(项目语言)；旧修复没有时一句话取 Issue 标题、原因取修复摘要、不写范围。文件清单为工作区的
改动，按文件名排序，与修复交接文档的 changedFiles 不一致时说明差异(apply 之后工作区有变化)。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain import release_format
from tightrein.domain.issue import Issue
from tightrein.pipeline.common.conventions import Conventions
from tightrein.vcs.operations import sorted_files


def message(config: ProjectConfig, conventions: Conventions, issue: Issue, fix: Mapping[str, Any]) -> str:
    written = fix.get("release") or {}
    kind = release_format.mapped(issue.task_type, config.get("git.commitTypes"))
    return release_format.commit_message(conventions.commit, kind=kind, scope=written.get("scope"),
                                         summary=written.get("subject") or issue.title,
                                         why=written.get("why") or fix.get("summary"))


def files(changed: Sequence[str], fix: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """返回按文件名排序的清单与差异说明。"""
    expected = {item["path"] for item in fix.get("changedFiles") or []}
    actual = set(changed)
    differences = [f"修复之后新增的改动：{path}" for path in sorted(actual - expected)]
    differences += [f"修复中改动过、现在没有改动：{path}" for path in sorted(expected - actual)]
    return list(sorted_files(list(actual))), differences
