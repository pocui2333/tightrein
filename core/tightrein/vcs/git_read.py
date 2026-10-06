"""git 的只读查询(architecture/02 4.3)。不需要确认；失败时抛出 VcsError 的子类。

`repo` 是项目主仓库或某个 worktree 的路径。只读查询中访问远程的只有 fetch，有单独的超时，遇到网络错误时按 process 的
规则重试。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.vcs import parse
from tightrein.vcs.errors import RefNotFound
from tightrein.vcs.parse import Commit, FilePatch, FileStat, RepoStatus, WorktreeInfo
from tightrein.vcs.process import GIT_FATAL_EXIT_CODE, VcsProcess

NO_QUOTE = ("-c", "core.quotePath=false")
OPERATION_STATE_FILES = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG")
NOT_FOUND_EXIT_CODE = 1


@dataclass(frozen=True)
class HeadState:
    commit: str | None
    branch: str | None


@dataclass(frozen=True)
class FileDiff:
    path: str
    added: int | None
    removed: int | None
    original_path: str | None = None
    added_lines: tuple[str, ...] = ()
    removed_lines: tuple[str, ...] = ()

    @property
    def binary(self) -> bool:
        return self.added is None


@dataclass(frozen=True)
class Diff:
    files: tuple[FileDiff, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files)

    @property
    def lines_added(self) -> int:
        return sum(item.added or 0 for item in self.files)

    @property
    def lines_removed(self) -> int:
        return sum(item.removed or 0 for item in self.files)


def _pathspec(paths: Sequence[str] | None) -> list[str]:
    return ["--", *paths] if paths else []


def _merge(stats: list[FileStat], patches: list[FilePatch]) -> Diff:
    by_path = {patch.path: patch for patch in patches}
    files = []
    for stat in stats:
        patch = by_path.get(stat.path) or by_path.get(stat.original_path or "") or FilePatch(stat.path)
        files.append(FileDiff(stat.path, stat.added, stat.removed, stat.original_path, patch.added, patch.removed))
    return Diff(tuple(files))


class GitReader:
    def __init__(self, process: VcsProcess) -> None:
        self.process = process

    def _git(self, repo: Path, *args: str, ok_codes: Sequence[int] = (0,), stdin: str | None = None) -> str:
        return self.process.git(repo, *args, stdin=stdin, ok_codes=ok_codes).stdout

    def status(self, repo: Path) -> RepoStatus:
        text = self._git(repo, "status", "--porcelain=v2", "--branch", "--untracked-files=all", "-z")
        return parse.parse_status(text)

    def head(self, repo: Path) -> HeadState:
        commit = self.process.git(repo, "rev-parse", "--verify", "--quiet", "HEAD", ok_codes=(0, NOT_FOUND_EXIT_CODE))
        branch = self.process.git(repo, "symbolic-ref", "-q", "HEAD", ok_codes=(0, NOT_FOUND_EXIT_CODE))
        name = branch.stdout.strip().removeprefix("refs/heads/") if branch.returncode == 0 else ""
        return HeadState(commit.stdout.strip() or None, name or None)

    def rev_parse(self, repo: Path, ref: str) -> str:
        """ref 指向的 commit；不存在时抛出 RefNotFound。"""
        result = self.process.git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                                  ok_codes=(0, NOT_FOUND_EXIT_CODE))
        if result.returncode != 0:
            raise RefNotFound(f"{ref} 不存在", argv=result.argv, returncode=result.returncode)
        return result.stdout.strip()

    def branch_exists(self, repo: Path, branch: str) -> bool:
        result = self.process.git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
                                  ok_codes=(0, NOT_FOUND_EXIT_CODE))
        return result.returncode == 0

    def refs(self, repo: Path) -> dict[str, str]:
        return parse.parse_refs(self._git(repo, "for-each-ref", f"--format={parse.REF_FORMAT}", "refs/heads",
                                          "refs/tags"))

    def remote_branches(self, repo: Path) -> list[str]:
        """远程跟踪分支的短名(不含远程名，如 origin/feat/x 记为 feat/x)，不含 HEAD。"""
        text = self._git(repo, "for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes")
        return [line for line in text.splitlines() if line and line != "HEAD"]

    def remotes(self, repo: Path) -> dict[str, tuple[str, ...]]:
        text = self._git(repo, "config", "-z", "--get-regexp", r"^remote\.", ok_codes=(0, NOT_FOUND_EXIT_CODE))
        return parse.parse_config(text)

    def local_config(self, repo: Path, pattern: str) -> dict[str, tuple[str, ...]]:
        """仓库本地配置(不含用户全局与系统配置)中键匹配 pattern 的项。"""
        return parse.parse_config(self._git(repo, "config", "--local", "-z", "--get-regexp", pattern,
                                            ok_codes=(0, NOT_FOUND_EXIT_CODE)))

    def stash(self, repo: Path) -> tuple[str, ...]:
        return tuple(self._git(repo, "stash", "list", "--format=%H").split())

    def git_dir(self, repo: Path) -> Path:
        return Path(self._git(repo, "rev-parse", "--absolute-git-dir").strip())

    def operations_in_progress(self, repo: Path) -> tuple[str, ...]:
        """worktree 的 git 目录下存在的合并、变基、拣选等状态文件。"""
        git_dir = self.git_dir(repo)
        return tuple(name for name in OPERATION_STATE_FILES if (git_dir / name).exists())

    def merge_head(self, repo: Path) -> str | None:
        result = self.process.git(repo, "rev-parse", "--verify", "--quiet", "MERGE_HEAD",
                                  ok_codes=(0, NOT_FOUND_EXIT_CODE))
        return result.stdout.strip() or None

    def conflict_files(self, repo: Path) -> tuple[str, ...]:
        text = self._git(repo, *NO_QUOTE, "diff", "--name-only", "--diff-filter=U", "-z")
        return tuple(sorted(set(parse.iter_nul(text))))

    def log(self, repo: Path, rev_range: str, paths: Sequence[str] | None = None,
            limit: int | None = None) -> list[Commit]:
        count = [f"--max-count={limit}"] if limit is not None else []
        text = self._git(repo, "log", "-z", f"--format={parse.LOG_FORMAT}", *count, rev_range, *_pathspec(paths))
        return parse.parse_log(text)

    def oneline(self, repo: Path, rev_range: str) -> str:
        """`git log --oneline` 的原样输出(PR 描述的 Commit 记录一段)。"""
        return self._git(repo, "log", "--oneline", "--no-decorate", rev_range)

    def blame(self, repo: Path, path: str, line_start: int, line_end: int, rev: str = "HEAD") -> list[parse.BlameLine]:
        text = self._git(repo, "blame", "--porcelain", "-L", f"{line_start},{line_end}", rev, "--", path)
        return parse.parse_blame(text)

    def diff(self, repo: Path, base: str, head: str | None = None, paths: Sequence[str] | None = None) -> Diff:
        """base 到 head(为空时为工作目录)的改动：每个文件的增删行数与新增、删除的行。"""
        revisions = [base] if head is None else [base, head]
        stats = parse.parse_numstat(self._git(repo, *NO_QUOTE, "diff", "--numstat", "-z", *revisions,
                                              *_pathspec(paths)))
        patch = self._git(repo, *NO_QUOTE, "diff", "--unified=0", "--no-color", "--no-ext-diff", "--src-prefix=a/",
                          "--dst-prefix=b/", *revisions, *_pathspec(paths))
        return _merge(stats, parse.parse_patch(patch))

    def show(self, repo: Path, rev: str, path: str) -> str | None:
        """文件在某个版本中的内容；该版本中没有这个文件时为空(冲突报告与取舍判定比较两侧版本)。"""
        result = self.process.git(repo, "show", f"{rev}:{path}", ok_codes=(0, GIT_FATAL_EXIT_CODE))
        return result.stdout if result.returncode == 0 else None

    def patch(self, repo: Path, base: str, head: str | None = None) -> str:
        """base 到 head(为空时为工作目录，不含未跟踪的文件)的统一格式 diff 原文：交给评审的改动、--output 模式的
        changes.patch 与运行即评测用例中的最终补丁。"""
        revisions = [base] if head is None else [base, head]
        return self._git(repo, *NO_QUOTE, "diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/",
                         *revisions)

    def untracked(self, repo: Path, include_ignored: bool = False) -> tuple[str, ...]:
        """未跟踪的文件；include_ignored 为假时不含被忽略的文件。"""
        exclude = [] if include_ignored else ["--exclude-standard"]
        return tuple(sorted(parse.iter_nul(self._git(repo, "ls-files", "--others", *exclude, "-z"))))

    def files(self, repo: Path) -> tuple[str, ...]:
        """已跟踪与未被忽略的未跟踪文件。"""
        text = self._git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        return tuple(sorted(set(parse.iter_nul(text))))

    def diff_hash(self, repo: Path, base: str) -> str:
        """base 到工作目录的改动的 sha256，作为提交操作的幂等键与前置条件：逐个改动的文件(含未跟踪的)记路径、
        base 中的模式与 blob、工作目录中的模式与内容的 blob。与改动是否已提交无关；合并只改了其他文件的 main 后
        以合并进来的 main 版本为基准，结果与合并前相同。"""
        tracked = parse.iter_nul(self._git(repo, *NO_QUOTE, "diff", "--name-only", "--no-renames", "-z", base))
        paths = sorted({*tracked, *self.untracked(repo)})
        before: dict[str, str] = {}
        if paths:
            for entry in parse.iter_nul(self._git(repo, *NO_QUOTE, "ls-tree", "-r", "-z", base, "--", *paths)):
                meta, _, path = entry.partition("\t")
                mode, _, blob = meta.split(" ")
                before[path] = f"{mode} {blob}"
        present = [path for path in paths if (repo / path).is_file()]
        blobs = self._git(repo, "hash-object", "--stdin-paths", stdin="".join(f"{path}\n" for path in present)).split() \
            if present else []
        after = {path: f"{_file_mode(repo / path)} {blob}" for path, blob in zip(present, blobs)}
        digest = hashlib.sha256()
        for path in paths:
            digest.update(f"{path}\0{before.get(path, '-')}\0{after.get(path, '-')}\0".encode("utf-8"))
        return digest.hexdigest()

    def merge_base(self, repo: Path, left: str, right: str) -> str:
        """两个版本的最近公共祖先。"""
        return self._git(repo, "merge-base", left, right).strip()

    def is_ancestor(self, repo: Path, commit: str, of: str) -> bool:
        result = self.process.git(repo, "merge-base", "--is-ancestor", commit, of,
                                  ok_codes=(0, NOT_FOUND_EXIT_CODE, GIT_FATAL_EXIT_CODE))
        if result.returncode == GIT_FATAL_EXIT_CODE:
            raise RefNotFound(f"{commit} 或 {of} 不存在", argv=result.argv, returncode=result.returncode)
        return result.returncode == 0

    def branches_containing(self, repo: Path, commit: str, remote: bool = True) -> list[str]:
        self.rev_parse(repo, commit)
        scope, prefix = (["-r"], "refs/remotes/") if remote else ([], "refs/heads/")
        text = self._git(repo, "branch", *scope, "--contains", commit, "--format=%(refname)")
        names = (line.removeprefix(prefix) for line in text.splitlines() if line.startswith(prefix))
        return sorted(name for name in names if not name.endswith("/HEAD"))

    def has_commit(self, repo: Path, ref: str) -> bool:
        """ref 指向的 commit 在本地仓库中已存在(不访问远程)。"""
        try:
            self.rev_parse(repo, ref)
        except RefNotFound:
            return False
        return True

    def fetch(self, repo: Path, prune: bool = False) -> None:
        """超时取 runtime.vcs.fetchTimeoutSeconds；失败或超时抛出 NetworkError(按只读查询重试)。"""
        self.process.git(repo, "fetch", "origin", *(["--prune"] if prune else []), retry=True,
                         timeout=self.process.fetch_timeout)

    def worktree_list(self, repo: Path) -> list[WorktreeInfo]:
        return parse.parse_worktrees(self._git(repo, "worktree", "list", "--porcelain", "-z"))


def _file_mode(path: Path) -> str:
    """git 记录的文件模式：符号链接、可执行文件或普通文件。"""
    if path.is_symlink():
        return "120000"
    return "100755" if os.access(path, os.X_OK) else "100644"
