"""GitHub Issue 镜像的 gh 调用(architecture/06 10.8)：远程地址解析、只读查询与写命令的参数。

只读查询经 VcsProcess.gh(retry=True)；写命令在这里只给出参数(不含可执行文件名)，由调用方按项目配置直接执行，
或放进待确认操作由执行器执行。正文一律以 `--body-file` 传入，不经命令行。所有命令都带 `--repo`，不依赖工作目录。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.domain.issue import GithubLink
from tightrein.vcs.process import VcsProcess

_REMOTE = re.compile(r"github\.com[:/]+(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_ISSUE_URL = re.compile(r"https://\S+/issues/(?P<number>\d+)")
COMPLETED = "completed"
NOT_PLANNED = "not planned"


def repo_slug(url: str) -> str | None:
    """GitHub 远程地址(https、ssh、scp 形式)中的 owner/name；不是 GitHub 地址时为 None。"""
    match = _REMOTE.search(url.strip())
    return match.group("slug") if match else None


def issue_link(stdout: str) -> GithubLink:
    """`gh issue create` 输出中的 Issue 链接；没有时抛出 ValueError。"""
    matches = list(_ISSUE_URL.finditer(stdout))
    if not matches:
        raise ValueError(f"gh issue create 的输出中没有 Issue 链接：{stdout.strip()[:200]}")
    return GithubLink(int(matches[-1].group("number")), matches[-1].group(0))


@dataclass(frozen=True)
class RepositoryFacts:
    private: bool
    issues_enabled: bool


@dataclass(frozen=True)
class RemoteIssue:
    """GitHub 上的开关状态：state 为 open 或 closed；reason 为 gh 的 stateReason(COMPLETED、NOT_PLANNED、DUPLICATE、
    REOPENED)，没有时为空。"""

    number: int
    state: str
    reason: str | None = None


class GhIssues:
    def __init__(self, process: VcsProcess, cwd: Path, limit: int) -> None:
        """cwd 为执行 gh 的目录(命令都带 --repo，只要求目录存在)；limit 为列出 Issue 与标签的条数
        (runtime.vcs.issueListLimit)。"""
        self.process = process
        self.cwd = cwd
        self.limit = limit

    def _json(self, *args: str) -> Any:
        return json.loads(self.process.gh(self.cwd, *args, retry=True).stdout or "null")

    def _items(self, *args: str) -> list[dict[str, Any]]:
        return list(self._json(*args, "--limit", str(self.limit)) or [])

    def repository(self, slug: str) -> RepositoryFacts:
        data = self._json("repo", "view", slug, "--json", "isPrivate,hasIssuesEnabled")
        return RepositoryFacts(bool(data["isPrivate"]), bool(data["hasIssuesEnabled"]))

    def labels(self, slug: str) -> set[str]:
        return {item["name"] for item in self._items("label", "list", "--repo", slug, "--json", "name")}

    def states(self, slug: str) -> dict[int, RemoteIssue]:
        items = self._items("issue", "list", "--repo", slug, "--state", "all", "--json", "number,state,stateReason")
        return {item["number"]: RemoteIssue(item["number"], item["state"].lower(), item.get("stateReason") or None)
                for item in items}

    def find_marker(self, slug: str, marker: str) -> GithubLink | None:
        """正文含有本地标记的 Issue(建 Issue 被中断后找回)；有多个时取编号最小的。"""
        items = self._items("issue", "list", "--repo", slug, "--state", "all", "--json", "number,url,body")
        found = sorted((item["number"], item["url"]) for item in items if marker in (item.get("body") or ""))
        return GithubLink(*found[0]) if found else None


# 写命令的参数(不含可执行文件名)


def create_args(slug: str, title: str, body_file: str, labels: Sequence[str], parent: int | None = None,
                blocked_by: int | None = None) -> tuple[str, ...]:
    """建 Issue；parent、blocked_by 为 GitHub 原生的子 Issue 与阻塞关系(gh 2.94 起支持)。"""
    relations = [*(("--parent", str(parent)) if parent is not None else ()),
                 *(("--blocked-by", str(blocked_by)) if blocked_by is not None else ())]
    return ("issue", "create", "--repo", slug, "--title", title, "--body-file", body_file,
            *(part for label in labels for part in ("--label", label)), *relations)


def edit_relations_args(slug: str, number: int, parent: int | None, blocked_by: int | None) -> tuple[str, ...]:
    """为已有镜像补上子 Issue 与阻塞关系。"""
    args = ["issue", "edit", str(number), "--repo", slug]
    if parent is not None:
        args += ["--parent", str(parent)]
    if blocked_by is not None:
        args += ["--add-blocked-by", str(blocked_by)]
    return tuple(args)


def edit_body_args(slug: str, number: int, title: str, body_file: str) -> tuple[str, ...]:
    return ("issue", "edit", str(number), "--repo", slug, "--title", title, "--body-file", body_file)


def edit_labels_args(slug: str, number: int, add: Sequence[str], remove: Sequence[str]) -> tuple[str, ...]:
    args = ["issue", "edit", str(number), "--repo", slug]
    if add:
        args += ["--add-label", ",".join(add)]
    if remove:
        args += ["--remove-label", ",".join(remove)]
    return tuple(args)


def comment_args(slug: str, number: int, body_file: str) -> tuple[str, ...]:
    return ("issue", "comment", str(number), "--repo", slug, "--body-file", body_file)


def close_args(slug: str, number: int, reason: str) -> tuple[str, ...]:
    return ("issue", "close", str(number), "--repo", slug, "--reason", reason)


def reopen_args(slug: str, number: int) -> tuple[str, ...]:
    return ("issue", "reopen", str(number), "--repo", slug)


def label_args(slug: str, name: str, color: str, description: str) -> tuple[str, ...]:
    return ("label", "create", name, "--repo", slug, "--color", color, "--description", description)
