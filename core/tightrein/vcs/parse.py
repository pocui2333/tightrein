"""git porcelain 输出与 gh `--json` 输出的解析(architecture/02 4.3)。只做文本到数据的转换，不执行命令。

git 的输出一律用 `-z` 或 `%x00` 分隔，路径中的空格、换行与非 ASCII 字符都不需要转义处理。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tightrein.domain.clock import parse_iso

NUL = "\0"
CHANGED = "changed"
RENAMED = "renamed"
UNMERGED = "unmerged"
UNTRACKED = "untracked"
IGNORED = "ignored"
LOG_FIELDS = ("%H", "%an", "%aI", "%s")
LOG_FORMAT = "%x00".join(LOG_FIELDS)
REF_FORMAT = "%(refname)%00%(objectname)"
_BLAME_HEADER = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")


@dataclass(frozen=True)
class StatusEntry:
    path: str
    kind: str
    index: str = "."
    worktree: str = "."
    original_path: str | None = None


@dataclass(frozen=True)
class RepoStatus:
    commit: str | None
    branch: str | None
    upstream: str | None
    ahead: int
    behind: int
    entries: tuple[StatusEntry, ...]

    @property
    def clean(self) -> bool:
        """没有暂存、未暂存与未跟踪的改动；被忽略的文件(构建产物)不影响。"""
        return all(entry.kind == IGNORED for entry in self.entries)

    @property
    def unmerged(self) -> tuple[str, ...]:
        return tuple(sorted(entry.path for entry in self.entries if entry.kind == UNMERGED))

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(sorted(entry.path for entry in self.entries if entry.kind != IGNORED))


def parse_status(text: str) -> RepoStatus:
    """`git status --porcelain=v2 --branch -z`。"""
    commit = branch = upstream = None
    ahead = behind = 0
    entries: list[StatusEntry] = []
    records = iter(text.split(NUL))
    for record in records:
        if not record:
            continue
        if record.startswith("# "):
            name, _, value = record[2:].partition(" ")
            if name == "branch.oid":
                commit = None if value == "(initial)" else value
            elif name == "branch.head":
                branch = None if value == "(detached)" else value
            elif name == "branch.upstream":
                upstream = value
            elif name == "branch.ab":
                plus, minus = value.split(" ")
                ahead, behind = int(plus[1:]), int(minus[1:])
            continue
        kind = record[0]
        if kind == "1":
            parts = record.split(" ", 8)
            entries.append(StatusEntry(parts[8], CHANGED, parts[1][0], parts[1][1]))
        elif kind == "2":
            parts = record.split(" ", 9)
            entries.append(StatusEntry(parts[9], RENAMED, parts[1][0], parts[1][1], next(records)))
        elif kind == "u":
            parts = record.split(" ", 10)
            entries.append(StatusEntry(parts[10], UNMERGED, parts[1][0], parts[1][1]))
        elif kind == "?":
            entries.append(StatusEntry(record[2:], UNTRACKED))
        elif kind == "!":
            entries.append(StatusEntry(record[2:], IGNORED))
        else:
            raise ValueError(f"无法识别的 status 条目：{record!r}")
    return RepoStatus(commit, branch, upstream, ahead, behind, tuple(entries))


def parse_refs(text: str) -> dict[str, str]:
    """`git for-each-ref --format=%(refname)%00%(objectname)`：引用名到 commit。"""
    refs: dict[str, str] = {}
    for line in text.splitlines():
        if line:
            name, _, commit = line.partition(NUL)
            refs[name] = commit
    return refs


def parse_config(text: str) -> dict[str, tuple[str, ...]]:
    """`git config -z --get-regexp`：键到值；同一个键可以有多个值(例如 remote.origin.fetch)。"""
    values: dict[str, list[str]] = {}
    for record in text.split(NUL):
        if record:
            key, _, value = record.partition("\n")
            values.setdefault(key, []).append(value)
    return {key: tuple(items) for key, items in values.items()}


@dataclass(frozen=True)
class WorktreeInfo:
    path: str
    head: str | None
    branch: str | None
    bare: bool = False
    detached: bool = False
    locked: bool = False
    prunable: bool = False


def parse_worktrees(text: str) -> list[WorktreeInfo]:
    """`git worktree list --porcelain -z`：属性以 NUL 分隔，worktree 之间多一个 NUL。"""
    worktrees: list[WorktreeInfo] = []
    current: dict[str, Any] = {}
    for record in [*text.split(NUL), ""]:
        if not record:
            if current:
                worktrees.append(WorktreeInfo(**current))
                current = {}
            continue
        name, _, value = record.partition(" ")
        if name == "worktree":
            current = {"path": value, "head": None, "branch": None}
        elif name == "HEAD":
            current["head"] = value
        elif name == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif name in ("bare", "detached", "locked", "prunable"):
            current[name] = True
    return worktrees


@dataclass(frozen=True)
class Commit:
    commit: str
    author: str
    time: datetime
    subject: str


def parse_log(text: str) -> list[Commit]:
    """`git log -z --format=<LOG_FORMAT>`：字段与记录都以 NUL 分隔。"""
    fields = text.split(NUL)
    if fields and fields[-1] == "":
        fields.pop()
    size = len(LOG_FIELDS)
    if len(fields) % size:
        raise ValueError(f"git log 输出的字段数 {len(fields)} 不是 {size} 的倍数")
    commits = []
    for start in range(0, len(fields), size):
        commit, author, when, subject = fields[start:start + size]
        commits.append(Commit(commit.lstrip("\n"), author, parse_iso(when), subject))
    return commits


@dataclass(frozen=True)
class BlameLine:
    line: int
    commit: str
    author: str
    email: str
    time: datetime
    content: str


def parse_blame(text: str) -> list[BlameLine]:
    """`git blame --porcelain`：同一 commit 的作者信息只在第一次出现时给出。"""
    info: dict[str, dict[str, str]] = {}
    lines: list[BlameLine] = []
    commit = ""
    final_line = 0
    for row in text.split("\n"):
        header = _BLAME_HEADER.match(row)
        if header:
            commit, final_line = header.group(1), int(header.group(3))
            info.setdefault(commit, {})
        elif row.startswith("\t"):
            details = info[commit]
            when = datetime.fromtimestamp(int(details["author-time"]), timezone.utc)
            email = details.get("author-mail", "").strip("<>")
            lines.append(BlameLine(final_line, commit, details.get("author", ""), email, when, row[1:]))
        elif row and commit:
            key, _, value = row.partition(" ")
            info[commit][key] = value
    return lines


@dataclass(frozen=True)
class FileStat:
    path: str
    added: int | None
    removed: int | None
    original_path: str | None = None

    @property
    def binary(self) -> bool:
        return self.added is None


def parse_numstat(text: str) -> list[FileStat]:
    """`git diff --numstat -z`：改名时路径为空，其后两条记录为原路径与新路径；二进制文件的行数为 `-`。"""
    stats: list[FileStat] = []
    records = iter(text.split(NUL))
    for record in records:
        if not record:
            continue
        added, removed, path = record.split("\t", 2)
        original = None
        if path == "":
            original, path = next(records), next(records)
        stats.append(FileStat(path, None if added == "-" else int(added), None if removed == "-" else int(removed),
                              original))
    return stats


@dataclass(frozen=True)
class FilePatch:
    """一个文件新增与删除的行的内容。"""

    path: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


def parse_patch(text: str) -> list[FilePatch]:
    """`git diff --unified=0 --no-color --no-ext-diff` 的输出按文件拆出新增与删除的行。"""
    patches: list[FilePatch] = []
    path: str | None = None
    added: list[str] = []
    removed: list[str] = []
    in_hunk = False

    def flush() -> None:
        if path is not None:
            patches.append(FilePatch(path, tuple(added), tuple(removed)))

    for row in text.split("\n"):
        if row.startswith("diff --git "):
            flush()
            path, added, removed, in_hunk = None, [], [], False
            continue
        if not in_hunk and row.startswith("--- "):
            if row != "--- /dev/null":
                path = row[len("--- a/"):]
            continue
        if not in_hunk and row.startswith("+++ "):
            if row != "+++ /dev/null":
                path = row[len("+++ b/"):]
            continue
        if row.startswith("@@ "):
            in_hunk = True
        elif in_hunk and row.startswith("+"):
            added.append(row[1:])
        elif in_hunk and row.startswith("-"):
            removed.append(row[1:])
    flush()
    return patches


@dataclass(frozen=True)
class PushResult:
    """`git push --porcelain` 中每个引用的状态标记：`!` 为被拒绝，`=` 为已是最新。"""

    refs: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)

    @property
    def rejected(self) -> tuple[str, ...]:
        return tuple(ref for flag, ref, _ in self.refs if flag == "!")


def parse_push(text: str) -> PushResult:
    refs = []
    for row in text.splitlines():
        if "\t" not in row or row.startswith("To "):
            continue
        flag, ref, summary = (row.split("\t") + [""])[:3]
        refs.append((flag, ref, summary))
    return PushResult(tuple(refs))


@dataclass(frozen=True)
class PullState:
    number: int
    url: str
    state: str
    mergeable: str | None = None
    merged_at: datetime | None = None
    merge_commit: str | None = None
    closed_at: datetime | None = None
    review_decision: str | None = None
    head_ref: str | None = None
    comments: tuple[Mapping[str, Any], ...] = ()
    reviews: tuple[Mapping[str, Any], ...] = ()


def _time(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


def parse_pull(item: Mapping[str, Any]) -> PullState:
    """gh 的 PR JSON；state 为 OPEN、CLOSED、MERGED，未返回的字段为空。"""
    merge_commit = item.get("mergeCommit")
    return PullState(
        number=item["number"], url=item["url"], state=item["state"], mergeable=item.get("mergeable") or None,
        merged_at=_time(item.get("mergedAt")), merge_commit=merge_commit.get("oid") if merge_commit else None,
        closed_at=_time(item.get("closedAt")), review_decision=item.get("reviewDecision") or None,
        head_ref=item.get("headRefName"), comments=tuple(item.get("comments") or ()),
        reviews=tuple(item.get("reviews") or ()),
    )


@dataclass(frozen=True)
class MergeFacts:
    """判断能否自动合并所需的 PR 字段(gh pr view --json state,isDraft,mergeable,mergeStateStatus,headRefOid,reviews)。"""

    state: str
    draft: bool
    mergeable: str | None
    merge_state: str | None
    head: str | None
    reviews: tuple[Mapping[str, Any], ...] = ()


def parse_merge_facts(item: Mapping[str, Any]) -> MergeFacts:
    return MergeFacts(state=item["state"], draft=bool(item.get("isDraft")), mergeable=item.get("mergeable") or None,
                      merge_state=item.get("mergeStateStatus") or None, head=item.get("headRefOid") or None,
                      reviews=tuple(item.get("reviews") or ()))


def parse_pulls(text: str) -> list[PullState]:
    return [parse_pull(item) for item in json.loads(text)]


def iter_nul(text: str) -> Iterator[str]:
    """以 NUL 分隔的路径列表(`git ls-files -z`、`git diff --name-only -z`)。"""
    return (item for item in text.split(NUL) if item)
