"""git 的只读查询与写操作(protocol/git.md)。git 只经这里调用。

- 以参数数组经 protocol/process.py 启动，不经 shell；固定加 `GIT_TERMINAL_PROMPT=0`(ssh、凭证助手不会等输入)与
  `LC_ALL=C`(英文输出才能按格式解析)；访问远程的命令加低速断开 `http.lowSpeedLimit/lowSpeedTime`，整体时限取
  limits.timeouts.git：网络卡住时 fetch 不会拖死整轮。
- 输出一律用 `-z`/`%x00` 分隔并加 `core.quotePath=false`：路径中的空格、换行与中文不需要转义处理。
- 错误只按退出码分类，不解析错误文字：访问远程的命令退出码 128 或超时为 NetworkError，其余非零为 CommandFailed；
  错误中的命令与错误输出先脱敏。只读的远程查询(fetch)在网络错误时按 limits.retry 退避重试，写操作绝不自动重试。
- 写操作都经 store/tables/operations.run_once，幂等键为「对象:步骤:操作:内容哈希」；上次执行被中断(InProgress)时
  先按实际状态对账：确认做完就补记完成，否则删键重做。程序永远不直接写主分支或游离 HEAD。
- 写操作的范围带原始输出目录(WriteScope.raw)时，这次操作期间(含复核与对账)的每条 git、gh 命令的输出(已脱敏)
  按顺序存到 `vcs/<对象>/<操作>/step-N.log`：任何一条失败即停，事后能看到停在哪一步、输出是什么。
- 前置条件：调用方在做决定时用 Git.state(GitHub.pull_state)记下观察到的状态，作为 expected 传给写操作；执行前以同样的
  项重新观察，任何一项不同即抛 Stale、不执行，防止对已变化的仓库执行旧决定。先查幂等键、再复核前置条件：
  已经做过的操作状态已变，先复核会被误判为过期。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tightrein.protocol.boundaries import Change
from tightrein.protocol.git.format import attribution_lines
from tightrein.protocol.limits import backoff_s
from tightrein.protocol.naming import Clock, parse_iso, segment
from tightrein.protocol.process import Command, Outcome, ProcessRunner
from tightrein.protocol.raw import RawDir
from tightrein.settings.load import Settings
from tightrein.store.tables import operations

if TYPE_CHECKING:
    from tightrein.protocol.security import Redactor

GIT = "git"
FIXED_ENV: Mapping[str, str] = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
NO_QUOTE = ("-c", "core.quotePath=false")
REMOTE_COMMANDS = frozenset({"fetch", "push", "pull", "ls-remote"})
FATAL_EXIT = 128
NOT_FOUND_EXIT = 1
CONFLICT_EXIT = 1
TIMEOUT_STOPS = frozenset({"timeout", "idle"})
NUL = "\0"
LOG_FIELDS = ("%H", "%an", "%aI", "%s")
LOG_FORMAT = "%x00".join(LOG_FIELDS)
UNTRACKED = "?"
# Git.state 能观察的前置条件：分支、HEAD、相对 base 的改动哈希、远程主干的 commit
STATE_KEYS = ("branch", "head", "diffHash", "originMain")
_BLAME_HEADER = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")
STEP_LOG_DIR = "vcs"
# 当前写操作的命令输出存放处：(原始输出目录, 相对目录)；由 once 设置、launch 写入。用 ContextVar 而不是 Git 的属性：
# 操作中经 Git.at 得到的另一个 Git、同一操作里的 gh 命令也记在同一处，并发的线程互不干扰。
_STEP_LOG: ContextVar[tuple[RawDir, str] | None] = ContextVar("_STEP_LOG", default=None)


# 错误


class GitError(Exception):
    """git、gh 调用失败；保留命令、退出码与错误输出的尾部(均已脱敏)。"""

    def __init__(self, message: str, *, argv: Sequence[str] = (), exit_code: int | None = None,
                 stderr: str = "") -> None:
        super().__init__(message)
        self.argv = tuple(argv)
        self.exit_code = exit_code
        self.stderr = stderr


class ProgramNotFound(GitError):
    """可执行文件不存在或无法启动。"""


class NetworkError(GitError):
    """访问远程失败：退出码 128 或超时。"""


class AuthError(GitError):
    """gh 未登录或权限不足(gh 退出码 4)，提示执行 gh auth login。"""


class CommandFailed(GitError):
    """其他非零退出。"""


class RefNotFound(GitError):
    """commit 或分支不存在。"""


class PushRejected(GitError):
    """远程拒绝推送(porcelain 标记 `!`)；先同步主干，绝不强推。"""

    def __init__(self, message: str, *, rejected: Sequence[str], argv: Sequence[str] = (),
                 exit_code: int | None = None, stderr: str = "") -> None:
        super().__init__(message, argv=argv, exit_code=exit_code, stderr=stderr)
        self.rejected = tuple(rejected)


class MergeConflict(GitError):
    """合并出现冲突；files 为冲突文件(已排序)，合并停在进行中，不自行取舍。"""

    def __init__(self, message: str, *, files: Sequence[str], argv: Sequence[str] = (),
                 exit_code: int | None = None, stderr: str = "") -> None:
        super().__init__(message, argv=argv, exit_code=exit_code, stderr=stderr)
        self.files = tuple(files)


class WorktreeDirty(GitError):
    """worktree 中有未提交的改动；paths 为改动的文件。"""

    def __init__(self, message: str, *, paths: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.paths = tuple(paths)


class MainBranchRefused(GitError):
    """提交、合并、推送作用于主分支或游离 HEAD：程序永远不直接写主分支。"""


class Stale(GitError):
    """前置条件已变：执行前重新观察到的状态与做决定时的不同，不执行；differences 为不同的项(已排序)。"""

    def __init__(self, operation: str, differences: Sequence[str]) -> None:
        super().__init__(f"{operation} 的前置条件已变化({'、'.join(differences)})，按过期处理，不执行")
        self.differences = tuple(differences)


# 类型


@dataclass(frozen=True)
class WriteScope:
    """写操作的幂等范围：对象(Issue、问题或运行的编号)与步骤(控制键)。"""

    conn: sqlite3.Connection
    clock: Clock
    subject: str
    point: str
    raw: RawDir | None = None  # 这一步的原始输出目录：给出时每条命令的输出按步存到 vcs/<对象>/<操作>/


@dataclass(frozen=True)
class Head:
    commit: str | None
    branch: str | None  # 游离 HEAD 时为 None


@dataclass(frozen=True)
class StatusEntry:
    path: str
    kind: str  # changed、renamed、unmerged、untracked
    original_path: str | None = None


@dataclass(frozen=True)
class RepoStatus:
    commit: str | None
    branch: str | None
    entries: tuple[StatusEntry, ...]

    @property
    def clean(self) -> bool:
        """没有暂存、未暂存与未跟踪的改动；被忽略的文件(构建产物)不列出，也就不影响。"""
        return not self.entries

    @property
    def unmerged(self) -> tuple[str, ...]:
        return tuple(sorted(entry.path for entry in self.entries if entry.kind == "unmerged"))

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(sorted(entry.path for entry in self.entries))


@dataclass(frozen=True)
class Commit:
    commit: str
    author: str
    time: datetime
    subject: str


@dataclass(frozen=True)
class BlameLine:
    line: int
    commit: str
    author: str
    time: datetime
    content: str


@dataclass(frozen=True)
class WorktreeInfo:
    path: str
    head: str | None
    branch: str | None
    detached: bool = False


# 公共函数(gh 与 git 共用)


def launch(runner: ProcessRunner, command: Command, *, remote: bool, redact: Callable[[str], str]) -> Outcome:
    """执行一条命令；启动失败与被终止转成带类型的错误，退出码交调用方判断。"""
    if not command.cwd.is_dir():
        raise CommandFailed(f"工作目录不存在：{command.cwd}", argv=command.argv)
    outcome = runner.run(command)
    described = redact(" ".join(command.argv))
    _log_step(described, outcome, redact)
    if outcome.start_error is not None:
        raise ProgramNotFound(f"无法启动 {command.argv[0]}：{outcome.start_error}", argv=command.argv)
    if outcome.stopped_by is not None:
        kind = NetworkError if remote and outcome.stopped_by in TIMEOUT_STOPS else CommandFailed
        raise kind(f"{described} 被终止({outcome.stopped_by})", argv=command.argv,
                   stderr=redact(outcome.stderr_tail))
    return outcome


def failure(kind: type[GitError], argv: Sequence[str], outcome: Outcome, redact: Callable[[str], str]) -> GitError:
    stderr = redact(outcome.stderr_tail.strip())
    return kind(f"{redact(' '.join(argv))} 退出码 {outcome.exit_code}：{stderr or '没有错误输出'}",
                argv=argv, exit_code=outcome.exit_code, stderr=stderr)


def retried[T](attempt: Callable[[], T], *, retries: int, settings: Settings, sleep: Callable[[float], None]) -> T:
    """只用于只读查询：网络错误时按 limits.backoff_s 等待后重试，最后一次的错误照常抛出。"""
    for number in range(1, retries + 1):
        try:
            return attempt()
        except NetworkError:
            sleep(backoff_s(number, settings=settings))
    return attempt()


def idempotency_key(scope: WriteScope, operation: str, content: Sequence[str]) -> str:
    digest = hashlib.sha256(NUL.join(content).encode()).hexdigest()[:16]
    return f"{scope.subject}:{scope.point}:{operation}:{digest}"


def once[T](scope: WriteScope, operation: str, content: Sequence[str], action: Callable[[], T],
         reconcile: Callable[[], T | None], *, expected: Mapping[str, str | None] | None = None,
         observe: Callable[[Sequence[str]], Mapping[str, str | None]] | None = None) -> T:
    """先查后做：做过的直接返回上次结果；上次被中断的先按实际状态对账，做完了补记完成，没做完删键重做。

    给出 expected(做决定时观察到的状态)时，真正执行前以同样的项调 observe 重新观察，有不同即抛 Stale(键随之删去，
    状态恢复后可以再做)。复核放在查键之后：已完成与对账补记的都直接返回，不复核。
    """
    key = idempotency_key(scope, operation, content)
    token = _STEP_LOG.set((scope.raw, f"{STEP_LOG_DIR}/{segment(scope.subject)}/{segment(operation)}")) \
        if scope.raw is not None else None
    try:
        return _once(scope, key, operation, action, reconcile, expected, observe)
    finally:
        if token is not None:
            _STEP_LOG.reset(token)


def _once[T](scope: WriteScope, key: str, operation: str, action: Callable[[], T], reconcile: Callable[[], T | None],
             expected: Mapping[str, str | None] | None,
             observe: Callable[[Sequence[str]], Mapping[str, str | None]] | None) -> T:
    def checked() -> T:
        if expected and observe is not None:
            differences = changed(expected, observe(tuple(expected)))
            if differences:
                raise Stale(operation, differences)
        return action()

    try:
        return operations.run_once(scope.conn, key, checked, scope.clock, subject=scope.subject, point=scope.point)
    except operations.InProgress:
        found = reconcile()
        if found is not None:
            operations.complete(scope.conn, key, found, scope.clock)
            return found
        operations.abandon(scope.conn, key)
        return operations.run_once(scope.conn, key, checked, scope.clock, subject=scope.subject, point=scope.point)


def _log_step(described: str, outcome: Outcome, redact: Callable[[str], str]) -> None:
    """在写操作期间时，把这条命令的输出存为下一个 step-N.log(编号接着同一目录中已有的，重做不覆盖上次的)。"""
    current = _STEP_LOG.get()
    if current is None:
        return
    raw, folder = current
    directory = raw.path(folder)
    number = len(list(directory.glob("step-*.log"))) + 1 if directory.is_dir() else 1
    result = (f"start_error: {outcome.start_error}" if outcome.start_error is not None
              else f"stopped_by: {outcome.stopped_by}" if outcome.stopped_by is not None
              else f"exit: {outcome.exit_code}")
    raw.write_text(f"{folder}/step-{number}.log",
                   f"$ {described}\n{result}\n--- stdout\n{redact(outcome.stdout)}\n--- stderr\n"
                   f"{redact(outcome.stderr_tail)}\n")


def changed(expected: Mapping[str, str | None], observed: Mapping[str, str | None]) -> list[str]:
    """expected 中与 observed 不同的项(已排序)；observed 中没有的项算不同。"""
    return sorted(name for name, value in expected.items() if name not in observed or observed[name] != value)


def iter_nul(text: str) -> list[str]:
    return [item for item in text.split(NUL) if item]


# Git


class Git:
    """一个仓库或 worktree 上的 git；`at` 得到同一配置下另一个目录(worktree)的 Git。"""

    def __init__(self, repo: Path, runner: ProcessRunner, env: Mapping[str, str], settings: Settings, *,
                 redactor: Redactor | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.repo = repo
        self.runner = runner
        self.env = {**env, **FIXED_ENV}
        self.settings = settings
        self.redactor = redactor
        self.sleep = sleep
        self.timeout_s = settings.duration("limits.timeouts.git")
        self.low_speed = ("-c", f"http.lowSpeedLimit={int(settings.get('limits.timeouts.gitLowSpeedBytes'))}",
                          "-c", f"http.lowSpeedTime={int(settings.duration('limits.timeouts.gitLowSpeedTime'))}")
        self.retries = settings.get("limits.retry.attempts")
        project = settings.project
        self.main_branch = project.main_branch if project is not None and project.main_branch else "main"

    def at(self, path: Path) -> Git:
        return Git(path, self.runner, self.env, self.settings, redactor=self.redactor, sleep=self.sleep)

    # 只读

    def status(self) -> RepoStatus:
        return _parse_status(self._out("status", "--porcelain=v2", "--branch", "--untracked-files=all", "-z"))

    def head(self) -> Head:
        commit = self._run("rev-parse", "--verify", "--quiet", "HEAD", ok=(0, NOT_FOUND_EXIT))
        branch = self._run("symbolic-ref", "-q", "HEAD", ok=(0, NOT_FOUND_EXIT))
        name = branch.stdout.strip().removeprefix("refs/heads/") if branch.exit_code == 0 else ""
        return Head(commit.stdout.strip() or None, name or None)

    def rev_parse(self, ref: str) -> str:
        found = self._run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", ok=(0, NOT_FOUND_EXIT))
        if found.exit_code != 0:
            raise RefNotFound(f"{ref} 不存在", argv=(GIT, "rev-parse", ref), exit_code=found.exit_code)
        return found.stdout.strip()

    def has_commit(self, ref: str) -> bool:
        """ref 指向的 commit 在本地已存在(不访问远程)。"""
        try:
            self.rev_parse(ref)
        except RefNotFound:
            return False
        return True

    def branch_exists(self, branch: str) -> bool:
        found = self._run("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", ok=(0, NOT_FOUND_EXIT))
        return found.exit_code == 0

    def remote_url(self, remote: str = "origin") -> str | None:
        found = self._run("config", "--get", f"remote.{remote}.url", ok=(0, NOT_FOUND_EXIT))
        return found.stdout.strip() or None

    def remote_branches(self) -> list[str]:
        """远程跟踪分支的短名(origin/feat/x 记为 feat/x)，不含 HEAD。"""
        text = self._out("for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes")
        return [line for line in text.splitlines() if line and line != "HEAD"]

    def merge_base(self, left: str, right: str) -> str:
        return self._out("merge-base", left, right).strip()

    def review_base(self, main_ref: str) -> str:
        """改动的比较基准：HEAD 与主干的最近公共祖先。合并过主干后它就是最近一次合并进来的主干版本，主干只改了
        别的文件时 diff_hash 与评审时相同，只需重新验证；改到同一文件或解决过冲突时才不同，需要重新审查。"""
        return self.merge_base("HEAD", main_ref)

    def is_ancestor(self, commit: str, of: str) -> bool | None:
        """commit 是否为 of 的祖先(同一 commit 算是)；任一方本地不存在时为 None(未知，不能当成否)。"""
        found = self._run("merge-base", "--is-ancestor", commit, of, ok=(0, NOT_FOUND_EXIT, FATAL_EXIT))
        if found.exit_code == FATAL_EXIT:
            return None
        return found.exit_code == 0

    def is_newer(self, commit: str | None, than: str | None) -> bool | None:
        """commit 是否严格晚于 than：than 是 commit 的祖先且两者不同；任一方为空或关系未知时为 None。"""
        if commit is None or than is None:
            return None
        if commit == than:
            return False
        return self.is_ancestor(than, commit)

    def merge_head(self) -> str | None:
        found = self._run("rev-parse", "--verify", "--quiet", "MERGE_HEAD", ok=(0, NOT_FOUND_EXIT))
        return found.stdout.strip() or None

    def diff(self, base: str, head: str | None = None, paths: Sequence[str] | None = None) -> str:
        """base 到 head(为空时为工作目录，不含未跟踪文件)的统一格式 diff 原文。"""
        revisions = [base] if head is None else [base, head]
        return self._out(*NO_QUOTE, "diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/",
                         *revisions, *_pathspec(paths))

    def changed_files(self, base: str) -> tuple[str, ...]:
        """base 到工作目录改动的文件，含未跟踪的新文件(被忽略的不算)。"""
        tracked = iter_nul(self._out(*NO_QUOTE, "diff", "--name-only", "--no-renames", "-z", base))
        return tuple(sorted({*tracked, *self.untracked()}))

    def numstat(self, base: str, head: str | None = None) -> list[Change]:
        """逐文件的增删行数。head 为空时比较工作目录，未跟踪的新文件也计入(全部行算新增)：改动量不能只看 git diff。
        二进制文件的行数记 0。"""
        revisions = [base] if head is None else [base, head]
        statuses = _name_status(self._out(*NO_QUOTE, "diff", "--name-status", "-z", *revisions))
        changes = [Change(path, added, deleted, statuses.get(path, "M"))
                   for path, added, deleted in _parse_numstat(self._out(*NO_QUOTE, "diff", "--numstat", "-z",
                                                                        *revisions))]
        if head is None:
            for path in self.untracked():
                file = self.repo / path
                if file.is_file() and not file.is_symlink():
                    with file.open("rb") as handle:
                        changes.append(Change(path, sum(1 for _ in handle), 0, UNTRACKED))
                else:
                    changes.append(Change(path, 0, 0, UNTRACKED))
        return sorted(changes, key=lambda change: change.path)

    def diff_hash(self, base: str) -> str:
        """base 到工作目录的改动的 sha256：逐个改动的文件(含未跟踪的)记路径、base 中的模式与 blob、工作目录中的模式与
        blob。与改动是否已提交无关(复现测试是新文件，提交后从未跟踪变成已跟踪，结果不变)，可作提交的幂等键。"""
        paths = self.changed_files(base)
        before: dict[str, str] = {}
        if paths:
            for entry in iter_nul(self._out(*NO_QUOTE, "ls-tree", "-r", "-z", base, "--", *paths)):
                meta, _, path = entry.partition("\t")
                mode, _, blob = meta.split(" ")
                before[path] = f"{mode} {blob}"
        present = [path for path in paths if (self.repo / path).is_file()]
        blobs = self._out("hash-object", "--stdin-paths", stdin="".join(f"{path}\n" for path in present)).split() \
            if present else []
        after = {path: f"{_file_mode(self.repo / path)} {blob}" for path, blob in zip(present, blobs)}
        digest = hashlib.sha256()
        for path in paths:
            digest.update(f"{path}\0{before.get(path, '-')}\0{after.get(path, '-')}\0".encode())
        return digest.hexdigest()

    def blame(self, path: str, start: int, end: int, rev: str = "HEAD") -> list[BlameLine]:
        return _parse_blame(self._out("blame", "--porcelain", "-L", f"{start},{end}", rev, "--", path))

    def log(self, rev_range: str, paths: Sequence[str] | None = None, limit: int | None = None) -> list[Commit]:
        count = [f"--max-count={limit}"] if limit is not None else []
        return _parse_log(self._out("log", "-z", f"--format={LOG_FORMAT}", *count, rev_range, *_pathspec(paths)))

    def show(self, rev: str, path: str) -> str | None:
        """文件在某个版本中的内容；该版本中没有这个文件时为 None。"""
        found = self._run("show", f"{rev}:{path}", ok=(0, FATAL_EXIT))
        return found.stdout if found.exit_code == 0 else None

    def ls_files(self) -> tuple[str, ...]:
        """已跟踪与未被忽略的未跟踪文件。"""
        return tuple(sorted(set(iter_nul(self._out("ls-files", "--cached", "--others", "--exclude-standard", "-z")))))

    def untracked(self) -> tuple[str, ...]:
        return tuple(sorted(iter_nul(self._out("ls-files", "--others", "--exclude-standard", "-z"))))

    def worktree_list(self) -> list[WorktreeInfo]:
        return _parse_worktrees(self._out("worktree", "list", "--porcelain", "-z"))

    def state(self, keys: Sequence[str] = STATE_KEYS, *, base: str = "HEAD") -> dict[str, str | None]:
        """做决定时观察的前置条件(写操作的 expected)：只取 keys 中的项；diffHash 相对 base，originMain 为
        origin/<主分支> 的 commit(本地没有时为 None)。只读本地状态，不 fetch：需要最新远程的在做决定前先 fetch。"""
        unknown = sorted(set(keys) - set(STATE_KEYS))
        if unknown:
            raise ValueError(f"不能观察的前置条件：{'、'.join(unknown)}；可用的有 {'、'.join(STATE_KEYS)}")
        state: dict[str, str | None] = {}
        if "branch" in keys or "head" in keys:
            head = self.head()
            state |= {name: value for name, value in (("branch", head.branch), ("head", head.commit)) if name in keys}
        if "diffHash" in keys:
            state["diffHash"] = self.diff_hash(base)
        if "originMain" in keys:
            main = f"refs/remotes/origin/{self.main_branch}"
            state["originMain"] = self.rev_parse(main) if self.has_commit(main) else None
        return state

    def fetch(self, *, prune: bool = False) -> None:
        """只读(只更新远程跟踪分支)，网络错误时按 limits.retry 重试；不记幂等键。"""
        self._run("fetch", "origin", *(["--prune"] if prune else []))

    # 写

    def worktree_add(self, path: Path, *, branch: str | None, base: str, scope: WriteScope) -> str:
        """从 base 新建 worktree：给出 branch 时 `-b` 建分支，否则为游离 HEAD(只读 worktree)。目录已存在且分支对应时
        直接复用：中断后重来不报错。新建前先 `worktree prune`：目录被删或挪走、登记还在时，git 会拒绝在同一路径新建；
        prune 只清目录已不在的登记，不动现存的 worktree 与分支。"""
        def matching() -> str | None:
            if not path.exists():
                return None
            head = self.at(path).head()
            return str(path) if head.branch == branch else None

        def action() -> str:
            found = matching()
            if found is not None:
                return found
            target = ("-b", branch) if branch is not None else ("--detach",)
            self._run("worktree", "prune", read=False)
            self._run("worktree", "add", *target, str(path), base, read=False)
            return str(path)

        return once(scope, "worktree-add", (str(path), branch or "", base), action, matching)

    def worktree_remove(self, path: Path, *, scope: WriteScope) -> bool:
        """先确认干净再删除(不加 --force)；目录已不在算完成。"""
        def gone() -> bool | None:
            return True if not path.exists() else None

        def action() -> bool:
            if not path.exists():
                return True
            status = self.at(path).status()
            if not status.clean:
                raise WorktreeDirty(f"{path} 有未提交的改动，不删除", paths=status.changed_paths)
            self._run("worktree", "remove", str(path), read=False)
            return True

        return once(scope, "worktree-remove", (str(path),), action, gone)

    def branch_delete(self, branch: str, *, scope: WriteScope) -> bool:
        """`git branch -d`：只删已合并的分支，从不用 -D；分支已不在算完成。"""
        def gone() -> bool | None:
            return True if not self.branch_exists(branch) else None

        def action() -> bool:
            if self.branch_exists(branch):
                self._run("branch", "-d", branch, read=False)
            return True

        return once(scope, "branch-delete", (branch,), action, gone)

    def commit(self, files: Sequence[str], message: str, *, base: str, scope: WriteScope,
               expected: Mapping[str, str | None] | None = None) -> str:
        """暂存 files 并以 `-F -`(提交信息从标准输入读入)提交，返回新的 HEAD。内容哈希取 base 到工作目录的 diff_hash：
        与是否已提交无关，提交前后算出的键相同。提交信息含 AI 署名时拒绝。expected 为 state(base=base) 的结果。"""
        if not files:
            raise ValueError("提交的文件清单不能为空")
        lines = attribution_lines(message)
        if lines:
            raise ValueError(f"提交信息不能带 AI 署名：{lines[0]}")
        self._refuse_main()
        listed = sorted(set(files))

        def committed() -> str | None:
            pending = self._run("diff", "--quiet", "HEAD", "--", *listed, ok=(0, 1)).exit_code
            untracked = set(self.untracked()) & set(listed)
            return self.head().commit if pending == 0 and not untracked else None

        def action() -> str:
            self._run("add", "--", *listed, read=False)
            text = message if message.endswith("\n") else message + "\n"
            self._run("commit", "-F", "-", stdin=text, read=False)
            return self.head().commit or ""

        return once(scope, "commit", (self.diff_hash(base), *listed), action, committed, expected=expected,
                    observe=lambda keys: self.state(keys, base=base))

    def merge(self, ref: str, *, scope: WriteScope, expected: Mapping[str, str | None] | None = None) -> str:
        """`merge --no-ff --no-edit`：不用 rebase、不改写已推送的历史。冲突时抛出 MergeConflict，合并停在进行中。"""
        branch = self._refuse_main()
        target = self.rev_parse(ref)

        def merged() -> str | None:
            if self.merge_head() is not None:
                return None
            return self.head().commit if self.is_ancestor(target, "HEAD") else None

        def action() -> str:
            found = self._run("merge", "--no-ff", "--no-edit", ref, ok=(0, CONFLICT_EXIT), read=False)
            if found.exit_code != 0:
                conflicts = self.status().unmerged
                if conflicts:
                    raise MergeConflict(f"合并 {ref} 出现冲突：{'、'.join(conflicts)}", files=conflicts,
                                        argv=(GIT, "merge", ref), exit_code=found.exit_code)
                raise failure(CommandFailed, (GIT, "merge", ref), found, self._redact)
            return self.head().commit or ""

        return once(scope, "merge", (branch, target), action, merged, expected=expected, observe=self.state)

    def revert(self, commit: str, *, scope: WriteScope, expected: Mapping[str, str | None] | None = None) -> str:
        """在当前分支上撤销 commit，返回新的 HEAD。合并提交(有第二个父提交)加 `-m 1` 取主干一侧；
        冲突时抛 MergeConflict，撤销停在进行中，不自行取舍。"""
        base = self.head().commit
        mainline = ("-m", "1") if self.has_commit(f"{commit}^2") else ()

        def reverted() -> str | None:
            head = self.head().commit
            return head if head != base and self.status().clean else None

        def action() -> str:
            found = self._run("revert", "--no-edit", *mainline, commit, ok=(0, CONFLICT_EXIT), read=False)
            if found.exit_code != 0:
                conflicts = sorted(self.status().unmerged)
                raise MergeConflict(f"撤销 {commit[:12]} 出现冲突：{'、'.join(conflicts)}", files=conflicts,
                                    argv=("git", "revert", commit), exit_code=found.exit_code)
            return self.head().commit or ""

        return once(scope, "revert", (self.head().branch or "", commit), action, reverted, expected=expected,
                    observe=self.state)

    def push(self, *, scope: WriteScope, expected: Mapping[str, str | None] | None = None) -> str:
        """`push --porcelain -u origin <当前分支>`；被拒按 porcelain 标记判断，绝不强推。返回推送的 commit。"""
        branch = self._refuse_main()
        commit = self.head().commit or ""

        def pushed() -> str | None:
            try:
                return commit if self.rev_parse(f"refs/remotes/origin/{branch}") == commit else None
            except RefNotFound:
                return None

        def action() -> str:
            found = self._run("push", "--porcelain", "-u", "origin", branch, ok=None, read=False)
            if found.exit_code == 0:
                return commit
            rejected = _rejected_refs(found.stdout)
            if rejected:
                raise PushRejected(f"远程拒绝推送 {'、'.join(rejected)}，先同步主干", rejected=rejected,
                                   argv=(GIT, "push", branch), exit_code=found.exit_code)
            kind = NetworkError if found.exit_code == FATAL_EXIT else CommandFailed
            raise failure(kind, (GIT, "push", "origin", branch), found, self._redact)

        return once(scope, "push", (branch, commit), action, pushed, expected=expected, observe=self.state)

    def checkout_detached(self, commit: str) -> None:
        """只移动只读 worktree 的游离 HEAD；重复执行结果相同，不记幂等键。"""
        self._run("checkout", "--quiet", "--detach", commit, read=False)

    # 内部

    def _refuse_main(self) -> str:
        branch = self.head().branch
        if branch is None or branch == self.main_branch:
            raise MainBranchRefused(f"{self.repo} 当前在{'游离 HEAD' if branch is None else '主分支 ' + branch}，"
                                    "程序不直接写主分支")
        return branch

    def _redact(self, text: str) -> str:
        return self.redactor.text(text) if self.redactor is not None else text

    def _out(self, *args: str, stdin: str | None = None) -> str:
        return self._run(*args, stdin=stdin).stdout

    def _run(self, *args: str, stdin: str | None = None, ok: Sequence[int] | None = (0,),
             read: bool = True) -> Outcome:
        """ok 为 None 时任何退出码都交调用方判断；read 为真且访问远程时网络错误按 limits.retry 重试。"""
        remote = bool(args) and args[0] in REMOTE_COMMANDS
        argv = (GIT, *(self.low_speed if remote else ()), *args)
        command = Command(argv=argv, cwd=self.repo, env=self.env, stdin=stdin, timeout_s=self.timeout_s)

        def attempt() -> Outcome:
            found = launch(self.runner, command, remote=remote, redact=self._redact)
            if ok is None or found.exit_code in ok:
                return found
            kind = NetworkError if remote and found.exit_code == FATAL_EXIT else CommandFailed
            raise failure(kind, argv, found, self._redact)

        return retried(attempt, retries=self.retries if read and remote else 0, settings=self.settings, sleep=self.sleep)


# 内部函数


def _pathspec(paths: Sequence[str] | None) -> list[str]:
    return ["--", *paths] if paths else []


def _file_mode(path: Path) -> str:
    if path.is_symlink():
        return "120000"
    return "100755" if os.access(path, os.X_OK) else "100644"


def _rejected_refs(text: str) -> tuple[str, ...]:
    """`git push --porcelain` 每个引用一行：`<标记>\\t<引用>\\t<摘要>`，`!` 为被拒绝。"""
    found = []
    for row in text.splitlines():
        if "\t" in row and not row.startswith("To "):
            flag, ref = row.split("\t")[:2]
            if flag == "!":
                found.append(ref)
    return tuple(found)


def _parse_status(text: str) -> RepoStatus:
    """`git status --porcelain=v2 --branch -z`。"""
    commit = branch = None
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
            continue
        kind = record[0]
        if kind == "1":
            entries.append(StatusEntry(record.split(" ", 8)[8], "changed"))
        elif kind == "2":
            entries.append(StatusEntry(record.split(" ", 9)[9], "renamed", next(records)))
        elif kind == "u":
            entries.append(StatusEntry(record.split(" ", 10)[10], "unmerged"))
        elif kind == "?":
            entries.append(StatusEntry(record[2:], "untracked"))
        else:
            raise ValueError(f"无法识别的 status 条目：{record!r}")
    return RepoStatus(commit, branch, tuple(entries))


def _parse_numstat(text: str) -> list[tuple[str, int, int]]:
    """`git diff --numstat -z`：改名时路径为空，其后两条记录为原路径与新路径；二进制文件的行数为 `-`。"""
    stats = []
    records = iter(text.split(NUL))
    for record in records:
        if not record:
            continue
        added, deleted, path = record.split("\t", 2)
        if path == "":
            next(records)
            path = next(records)
        stats.append((path, 0 if added == "-" else int(added), 0 if deleted == "-" else int(deleted)))
    return stats


def _name_status(text: str) -> dict[str, str]:
    """`git diff --name-status -z`：状态字母后跟路径；改名(R)与复制(C)后跟原路径与新路径。"""
    statuses: dict[str, str] = {}
    records = iter(text.split(NUL))
    for record in records:
        if not record:
            continue
        letter = record[0]
        if letter in "RC":
            next(records)
        statuses[next(records)] = letter
    return statuses


def _parse_log(text: str) -> list[Commit]:
    fields = text.split(NUL)
    if fields and fields[-1] == "":
        fields.pop()
    size = len(LOG_FIELDS)
    if len(fields) % size:
        raise ValueError(f"git log 输出的字段数 {len(fields)} 不是 {size} 的倍数")
    return [Commit(fields[start].lstrip("\n"), fields[start + 1], parse_iso(fields[start + 2]), fields[start + 3])
            for start in range(0, len(fields), size)]


def _parse_blame(text: str) -> list[BlameLine]:
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
            when = datetime.fromtimestamp(int(details["author-time"]), UTC)
            lines.append(BlameLine(final_line, commit, details.get("author", ""), when, row[1:]))
        elif row and commit:
            key, _, value = row.partition(" ")
            info[commit][key] = value
    return lines


def _parse_worktrees(text: str) -> list[WorktreeInfo]:
    """`git worktree list --porcelain -z`：属性以 NUL 分隔，worktree 之间多一个 NUL。"""
    worktrees: list[WorktreeInfo] = []
    path: str | None = None
    head = branch = None
    detached = False
    for record in [*text.split(NUL), ""]:
        if not record:
            if path is not None:
                worktrees.append(WorktreeInfo(path, head, branch, detached))
            path, head, branch, detached = None, None, None, False
            continue
        name, _, value = record.partition(" ")
        if name == "worktree":
            path = value
        elif name == "HEAD":
            head = value
        elif name == "branch":
            branch = value.removeprefix("refs/heads/")
        elif name == "detached":
            detached = True
    return worktrees
