"""安全(protocol/security.md)：子进程环境变量、脱敏、凭据读取、外部内容的边界、快照比对、启动 agent 前的凭据检查。

这里的规则写死，不可由 settings 覆盖。

快照比对(调用 agent 前后各取一次，比较差异而不是绝对状态：上一轮留下的未提交改动不算越界)：
- git 快照(snapshot、compare)：HEAD 与分支、本地分支与标签、远程配置、stash、worktree 列表、进行中的合并/变基/拣选
  状态文件，以及工作树内容(status、相对 HEAD 的 diff、未跟踪文件的内容)。分支不同为切换；分支相同而 commit 不同为
  新建提交；游离 HEAD 的 commit 变化时，新 commit 以原 commit 为祖先的算新建提交，否则算切换。当前分支随提交前进
  已由 HEAD 一项报告，本地分支一项不重复报。
- 文件快照(file_snapshot、changed_files)：worktree 之外 agent 不可写的路径(工作区配置、tightrein 自身代码)，记大小、
  mtime 与 sha256；再取时大小与 mtime 都没变的沿用上次的哈希不再读内容；遍历时不进入虚拟环境、缓存与依赖目录。
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tightrein.protocol.boundaries import matching_pattern
from tightrein.protocol.process import Command, ProcessRunner

PROXY_NAMES = frozenset({"http_proxy", "https_proxy", "all_proxy", "no_proxy",
                         "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})
# 代理变量保留：需要经代理访问模型服务的网络里，去掉它们 agent 工具的请求会被拒
ENV_ALLOW: frozenset[str] = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL"}) | PROXY_NAMES
SENSITIVE_NAME_PARTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "KEY", "CREDENTIAL", "AUTH", "COOKIE", "SESSION")
# claude 见到 ANTHROPIC_API_KEY 会改走按量计费的 API，而不是订阅；即使调用方显式给出也去掉
ALWAYS_REMOVED = frozenset({"ANTHROPIC_API_KEY"})
FIXED_ENV: Mapping[str, str] = {"GIT_TERMINAL_PROMPT": "0"}  # git 不卡在等密码输入
READ_ONLY_ENV: Mapping[str, str] = {"GIT_OPTIONAL_LOCKS": "0"}  # 只读 worktree 里 git status 不写 index 锁

REDACTED = "[REDACTED:{kind}]"
SECRET_KIND = "secret"
CREDENTIAL_KIND = "credential"

SENSITIVE_WORDS = frozenset({
    "password", "passwd", "pwd", "passphrase", "secret", "token", "jwt", "authorization", "cookie", "credential",
    "credentials",
})
SENSITIVE_COMPOUNDS = ("apikey", "accesskey", "privatekey", "connectionstring")

EXTERNAL_LIMIT = 20_000
SNAPSHOT_TIMEOUT_S = 600.0  # 与 limits.md 的 git 整体时限一致
NOT_FOUND_EXIT = 1  # git config --get-regexp 没有匹配、merge-base --is-ancestor 不是祖先
# worktree 的 git 目录下这些文件存在，说明有进行中的合并、拣选、撤销、变基或二分查找
OPERATION_STATE_FILES = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG")
# 仓库本地配置中可能带凭据的项：远程地址与凭证助手
CREDENTIAL_CONFIG = r"^(remote\..*\.(url|pushurl)|credential\..*)$"
# 文件快照不进入的目录：虚拟环境、缓存与依赖目录(体积大、运行中本就会变)
SKIPPED_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                          ".tox", "node_modules"})
SKIPPED_SUFFIXES = (".egg-info",)
SHORT = 12

_KEY_WORDS = (
    r"(?:password|passwd|pwd|passphrase|secret|token|jwt|authorization|cookie|credentials?"
    r"|api[_-]?key|access[_-]?key|private[_-]?key)"
)

# 每条规则线性时间：可变长的前缀只从一段字符的开头尝试(\b 或前面不是同类字符)，不在长串的每个位置回溯；
# 日志与会话记录中常见几十万字符的单行，平方级的规则会让写盘卡住
CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"://[^\s/@:]+:[^\s/@]+@"), "://" + REDACTED.format(kind="password") + "@"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]+=*"), r"\1 " + REDACTED.format(kind="auth")),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED.format(kind="jwt")),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), REDACTED.format(kind="api_key")),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED.format(kind="github_token")),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED.format(kind="github_token")),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED.format(kind="aws_key")),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED.format(kind="slack_token")),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     REDACTED.format(kind="private_key")),
)
# `password=…`、`"token": "…"`：只替换值、保留键名，便于追查是哪一项；已替换过的值不再处理(幂等)
KEY_VALUE_PATTERN = re.compile(
    rf"""(?i)(["']?(?<![\w.-])(?=[\w.-]*?{_KEY_WORDS})[\w.-]+["']?\s*[:=]\s*)(?!\[REDACTED:)"""
    r"""(?:"([^"]*)"|'([^']*)'|([^\s,;&"'}\]]+))"""
)
PERSONAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
     REDACTED.format(kind="id_number")),
    (re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"), REDACTED.format(kind="phone")),
)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # 保留制表符与换行
_EXTERNAL_CLOSE = re.compile(r"(?i)</(\s*external)")
_KEY_WORD_SPLIT = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]")


class SecretsPermission(Exception):
    """secrets.json 的权限不是 600：别的用户可能读到，拒绝运行。"""

    def __init__(self, path: Path, mode: int) -> None:
        self.path = path
        self.mode = mode
        super().__init__(f"{path} 的权限为 {mode:o}，须为 600(chmod 600 {path})")


class SecretsInvalid(Exception):
    """secrets.json 的格式不对；信息只带文件与条目名，不带值。"""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}：{reason}")


class SnapshotFailed(Exception):
    """git 状态读不了：无法做前后比对就不能放行 agent。"""


class Secrets(dict[str, str]):
    """读出的秘密值；repr、str 只列条目名，误打印也不泄露。"""

    def __repr__(self) -> str:
        return f"<secrets {', '.join(sorted(self))}>"

    __str__ = __repr__


@dataclass(frozen=True)
class TreeSnapshot:
    """一个仓库或 worktree 的 git 状态。tree_hash 只覆盖工作树内容(改动与未跟踪文件)，HEAD 与各引用单独比较。"""

    status: str
    tree_hash: str
    head: str | None = None
    branch: str | None = None  # 游离 HEAD 时为 None
    refs: tuple[tuple[str, str], ...] = ()  # 本地分支与标签 → commit
    remotes: tuple[tuple[str, str], ...] = ()  # remote.* 配置项 → 值
    stash: tuple[str, ...] = ()
    worktrees: tuple[tuple[str, str, bool], ...] = ()  # (路径, 分支, 是否锁定)；本 worktree 的分支由 HEAD 一项比较，记为空
    operations: tuple[str, ...] = ()  # 存在的 OPERATION_STATE_FILES


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int
    digest: str


class Redactor:
    """一个进程共用一个实例：读到的秘密值登记到这里，之后写出的任何内容都会替换掉它。"""

    def __init__(self, sensitive_keys: Iterable[str] = ()) -> None:
        self._extra_keys = frozenset(_normalize(key) for key in sensitive_keys)
        self._secrets: dict[str, str] = {}
        self._secret_pattern: re.Pattern[str] | None = None

    def register(self, value: str, kind: str = SECRET_KIND) -> None:
        if not value or value in self._secrets:
            return
        self._secrets[value] = kind
        # 长的先替换：一个值是另一个值的一部分时不留残片
        ordered = sorted(self._secrets, key=lambda secret: (-len(secret), secret))
        self._secret_pattern = re.compile("|".join(re.escape(secret) for secret in ordered))

    def text(self, value: str) -> str:
        if self._secret_pattern is not None:
            value = self._secret_pattern.sub(lambda match: REDACTED.format(kind=self._secrets[match.group()]), value)
        for pattern, replacement in CREDENTIAL_PATTERNS:
            value = pattern.sub(replacement, value)
        value = KEY_VALUE_PATTERN.sub(_key_value, value)
        for pattern, replacement in PERSONAL_PATTERNS:
            value = pattern.sub(replacement, value)
        return value

    def mapping(self, value: Any) -> Any:
        """递归处理映射、列表与字符串，返回新对象；键名敏感的值整体替换，其他标量原样返回。"""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {
                key: REDACTED.format(kind=CREDENTIAL_KIND)
                if item is not None and isinstance(key, str) and self.is_sensitive_key(key) else self.mapping(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.mapping(item) for item in value]
        return value

    def is_sensitive_key(self, key: str) -> bool:
        """按单词拆分(识别驼峰与分隔符)：accessToken、client_secret 是，input_tokens、keychain 不是。"""
        normalized = _normalize(key)
        if normalized in self._extra_keys or _words(key) & SENSITIVE_WORDS:
            return True
        return any(compound in normalized for compound in SENSITIVE_COMPOUNDS)


def child_env(environ: Mapping[str, str], *, extra_allowed: Iterable[str] = (), set_values: Mapping[str, str] = {},
              read_only: bool = False) -> dict[str, str]:
    """子进程的环境变量：白名单保留，再按名称与取值兜底去掉凭据。

    extra_allowed 为工具必需的变量名(写在 agents/tools/<工具>.md)，同样要过兜底；set_values 为程序显式给出的值
    (setup.json 中声明要注入的凭据等)，不过兜底。
    """
    allowed = ENV_ALLOW | frozenset(extra_allowed)
    env = {name: value for name, value in environ.items() if name in allowed and not _suspicious(name, value)}
    env.update(set_values)
    env.update(FIXED_ENV)
    if read_only:
        env.update(READ_ONLY_ENV)
    for name in ALWAYS_REMOVED:
        env.pop(name, None)
    return env


def sensitive_name(name: str) -> bool:
    upper = name.upper()
    return any(part in upper for part in SENSITIVE_NAME_PARTS)


def looks_like_credential(value: str) -> bool:
    return any(pattern.search(value) for pattern, _ in CREDENTIAL_PATTERNS) or bool(KEY_VALUE_PATTERN.search(value))


def load_secrets(path: Path, redactor: Redactor) -> dict[str, str]:
    """读一份 secrets.json(条目名 → 字符串值)；文件不存在为空。读到即登记到 redactor。"""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return Secrets()
    if mode != 0o600:
        raise SecretsPermission(path, mode)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        # JSONDecodeError 的信息只有位置，不含内容
        raise SecretsInvalid(path, f"不是合法的 JSON：{type(error).__name__}") from None
    if not isinstance(data, dict):
        raise SecretsInvalid(path, "顶层须为对象：条目名 → 值")
    wrong = sorted(name for name, value in data.items() if not isinstance(value, str))
    if wrong:
        raise SecretsInvalid(path, f"这些条目的值不是字符串：{', '.join(wrong)}")
    for value in data.values():
        redactor.register(value)
    return Secrets(data)


def external(source: str, text: str, *, limit: int = EXTERNAL_LIMIT) -> str:
    """外部内容放进提示时包在带来源的边界里：去控制字符、截断，内容中的闭合标签改写，不能提前跳出边界。"""
    cleaned = _CONTROL_CHARACTERS.sub("", text)
    if len(cleaned) > limit:
        cleaned = f"{cleaned[:limit]}\n[已截断：原文 {len(cleaned)} 字符，保留前 {limit} 字符]"
    cleaned = _EXTERNAL_CLOSE.sub(r"<\\/\1", cleaned)
    label = html.escape(_CONTROL_CHARACTERS.sub("", source), quote=True)
    return f'<external source="{label}">\n{cleaned}\n</external>'


def snapshot(repo: Path, runner: ProcessRunner) -> TreeSnapshot:
    """调用 agent 前后各取一次的 git 状态(见模块说明)；读不了时抛 SnapshotFailed(不能比对就不能放行)。

    status 去掉 `# branch.ab`(相对上游的领先落后数)：程序自己 fetch 会改变它，不是 agent 的写入。
    """
    env = _snapshot_env()
    status = _git(runner, repo, env, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all")
    records = [record for record in status.split("\0") if not record.startswith("# branch.ab ")]
    status = "\0".join(records)
    diff = _git(runner, repo, env, "diff", "--binary", "--no-ext-diff", "--no-color", "HEAD")
    digest = hashlib.sha256()
    digest.update("\0".join(record for record in records if not record.startswith("# ")).encode("utf-8"))
    digest.update(b"\0")
    digest.update(diff.encode("utf-8"))
    for path in _untracked(status):
        digest.update(f"\0{path}\0{_file_digest(repo / path)}".encode())
    head, branch = _head_of(records)
    refs = _git(runner, repo, env, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads", "refs/tags")
    remotes = _git(runner, repo, env, "config", "-z", "--get-regexp", r"^remote\.", ok=(0, NOT_FOUND_EXIT))
    stash = _git(runner, repo, env, "stash", "list", "--format=%H")
    worktrees = _git(runner, repo, env, "worktree", "list", "--porcelain", "-z")
    git_dir = Path(_git(runner, repo, env, "rev-parse", "--absolute-git-dir").strip())
    return TreeSnapshot(
        status, digest.hexdigest(), head, branch,
        refs=tuple(sorted((name, commit) for name, _, commit in (line.partition("\0") for line in refs.splitlines())
                          if commit)),
        remotes=tuple(sorted(_config(remotes))), stash=tuple(stash.split()),
        worktrees=_worktrees(worktrees, repo),
        operations=tuple(name for name in OPERATION_STATE_FILES if (git_dir / name).exists()),
    )


def compare(before: TreeSnapshot, after: TreeSnapshot, repo: Path, runner: ProcessRunner) -> list[str]:
    """前后两次 snapshot 的差异，每项一句说明；没有差异时为空。"""
    changes = _head_change(before, after, repo, runner)
    own = f"refs/heads/{before.branch}" if before.branch is not None and before.branch == after.branch else None
    old, new = dict(before.refs), dict(after.refs)
    for name in sorted(set(old) | set(new)):
        if name != own and old.get(name) != new.get(name):
            changes.append(f"引用 {name} 从 {_short(old.get(name))} 变为 {_short(new.get(name))}")
    if before.remotes != after.remotes:
        keys = sorted({key for key, _ in set(before.remotes) ^ set(after.remotes)})
        changes.append(f"远程配置被修改：{'、'.join(keys)}")
    if before.stash != after.stash:
        changes.append(f"stash 从 {len(before.stash)} 条变为 {len(after.stash)} 条")
    if before.worktrees != after.worktrees:
        changes.append("worktree 列表被修改")
    started = sorted(set(after.operations) - set(before.operations))
    if started:
        changes.append(f"开始了未完成的 git 操作：{'、'.join(started)}")
    if before.tree_hash != after.tree_hash:
        changes.append("工作树的内容有变化")
    return changes


def credentials_present(repo: Path, runner: ProcessRunner, patterns: Sequence[str]) -> list[str]:
    """启动 agent 前的检查：worktree 中未跟踪(含被忽略)且匹配凭据文件模式的文件，以及仓库本地配置中带口令或 token 的
    远程地址、凭证助手。只查本地配置，不查用户全局与系统配置(macOS 的系统配置自带 osxkeychain 凭证助手)。
    读不了时抛 SnapshotFailed。说明中不带凭据的值。"""
    env = _snapshot_env()
    found: list[str] = []
    for path in sorted(item for item in _git(runner, repo, env, "ls-files", "--others", "-z").split("\0") if item):
        pattern = matching_pattern(path, patterns)
        if pattern is not None:
            found.append(f"worktree 中有未跟踪的凭据文件 {path}(匹配 {pattern})，删除或移出 worktree 后重试")
    config = _git(runner, repo, env, "config", "--local", "-z", "--get-regexp", CREDENTIAL_CONFIG,
                  ok=(0, NOT_FOUND_EXIT))
    values: dict[str, list[str]] = {}
    for key, value in _config(config):
        values.setdefault(key, []).append(value)
    for key, items in sorted(values.items()):
        if key.startswith("credential."):
            found.append(f"仓库本地配置了凭证助手 {key}，用 git config --local --unset-all {key} 删除")
        elif any(_url_has_credentials(value) for value in items):
            found.append(f"仓库本地配置 {key} 中含有口令或 token，改为不带凭据的地址")
    return found


def file_snapshot(roots: Iterable[Path], previous: Mapping[str, FileState] | None = None) -> dict[str, FileState]:
    """roots(目录或文件)下全部文件与符号链接的状态，键为绝对路径；不跟随符号链接，不进入 SKIPPED_DIRS。
    previous 中大小与 mtime 都没变的文件沿用其哈希，不再读内容。"""
    states: dict[str, FileState] = {}
    for root in roots:
        for path in _walk(root):
            state = _file_state(path, (previous or {}).get(str(path)))
            if state is not None:
                states[str(path)] = state
    return states


def changed_files(before: Mapping[str, FileState], after: Mapping[str, FileState]) -> list[str]:
    """新增、删除或内容变化的文件(已排序)。"""
    return sorted(path for path in set(before) | set(after)
                  if path not in before or path not in after or before[path].digest != after[path].digest)


def _suspicious(name: str, value: str) -> bool:
    return sensitive_name(name) or looks_like_credential(value) or _proxy_with_credentials(name, value)


def _proxy_with_credentials(name: str, value: str) -> bool:
    """代理地址带用户名或密码时视为凭据。"""
    return name in PROXY_NAMES and "://" in value and urlsplit(value).username is not None


def _key_value(match: re.Match[str]) -> str:
    prefix = match.group(1)
    marker = REDACTED.format(kind=CREDENTIAL_KIND)
    if match.group(2) is not None:
        return f'{prefix}"{marker}"'
    if match.group(3) is not None:
        return f"{prefix}'{marker}'"
    return f"{prefix}{marker}"


def _normalize(key: str) -> str:
    return _NOT_ALPHANUMERIC.sub("", key.lower())


def _words(key: str) -> set[str]:
    return {word.lower() for word in _KEY_WORD_SPLIT.findall(key)}


def _snapshot_env() -> dict[str, str]:
    return child_env(os.environ, set_values={"LC_ALL": "C"}, read_only=True)


def _git(runner: ProcessRunner, repo: Path, env: Mapping[str, str], *args: str, ok: Sequence[int] = (0,)) -> str:
    outcome = runner.run(Command(("git", *args), repo, env, timeout_s=SNAPSHOT_TIMEOUT_S))
    if outcome.start_error is not None:
        raise SnapshotFailed(f"无法启动 git：{outcome.start_error}")
    if outcome.exit_code not in ok:
        reason = outcome.stopped_by or f"退出码 {outcome.exit_code}"
        raise SnapshotFailed(f"git {args[0]} 失败({reason})：{outcome.stderr_tail}")
    return outcome.stdout


def _untracked(status: str) -> list[str]:
    """porcelain v2 -z 中的未跟踪文件；改名记录(以 2 开头)后面多一段原路径，要跳过。"""
    paths: list[str] = []
    records = iter(status.split("\0"))
    for record in records:
        if record.startswith("? "):
            paths.append(record[2:])
        elif record.startswith("2 "):
            next(records, None)
    return paths


def _file_digest(path: Path) -> str:
    """符号链接记目标文本、不跟随；读不了内容的以「大小:mtime」代替；取状态与读取之间被删的记为 missing。"""
    try:
        if path.is_symlink():
            return f"link:{os.readlink(path)}"
        with open(path, "rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        try:
            state = path.lstat()
        except OSError:
            return "missing"
        return f"{state.st_size}:{state.st_mtime_ns}"


def _head_of(records: Sequence[str]) -> tuple[str | None, str | None]:
    """porcelain v2 的 `# branch.oid`、`# branch.head`：还没有提交时 commit 为 None，游离 HEAD 时分支为 None。"""
    head = branch = None
    for record in records:
        if record.startswith("# branch.oid "):
            value = record.removeprefix("# branch.oid ")
            head = None if value == "(initial)" else value
        elif record.startswith("# branch.head "):
            value = record.removeprefix("# branch.head ")
            branch = None if value == "(detached)" else value
    return head, branch


def _head_change(before: TreeSnapshot, after: TreeSnapshot, repo: Path, runner: ProcessRunner) -> list[str]:
    if before.branch != after.branch:
        return [f"分支从 {before.branch or '游离 HEAD'} 切换为 {after.branch or '游离 HEAD'}"]
    if before.head == after.head:
        return []
    created = before.branch is not None
    if not created and before.head is not None and after.head is not None:
        created = _is_ancestor(runner, repo, before.head, after.head)
    if created:
        return [(f"新建了提交 {after.head}(原 HEAD {before.head})；不会自动撤销，如需撤销由用户执行 "
                 f"git reset --soft {before.head}")]
    return [f"HEAD 从 {_short(before.head)} 切换到 {_short(after.head)}"]


def _is_ancestor(runner: ProcessRunner, repo: Path, commit: str, of: str) -> bool:
    """查不到的 commit(退出码 128)按不是祖先处理：报为切换，不会漏报。"""
    outcome = runner.run(Command(("git", "merge-base", "--is-ancestor", commit, of), repo, _snapshot_env(),
                                 timeout_s=SNAPSHOT_TIMEOUT_S))
    return outcome.start_error is None and outcome.stopped_by is None and outcome.exit_code == 0


def _short(commit: str | None) -> str:
    return commit[:SHORT] if commit else "(无)"


def _config(text: str) -> list[tuple[str, str]]:
    """`git config -z --get-regexp`：每条为「键\\n值」，同一个键可以有多个值。"""
    pairs = []
    for record in text.split("\0"):
        if record:
            key, _, value = record.partition("\n")
            pairs.append((key, value))
    return pairs


def _worktrees(text: str, repo: Path) -> tuple[tuple[str, str, bool], ...]:
    """`git worktree list --porcelain -z`：属性以 NUL 分隔，worktree 之间多一个 NUL。"""
    own = os.path.realpath(repo)
    found: list[tuple[str, str, bool]] = []
    path: str | None = None
    branch, locked = "", False
    for record in [*text.split("\0"), ""]:
        if not record:
            if path is not None:
                found.append((path, "" if os.path.realpath(path) == own else branch, locked))
            path, branch, locked = None, "", False
            continue
        name, _, value = record.partition(" ")
        if name == "worktree":
            path = value
        elif name == "branch":
            branch = value
        elif name == "locked":
            locked = True
    return tuple(sorted(found))


def _url_has_credentials(url: str) -> bool:
    """地址中带口令，或用户名本身像凭据(token 作用户名：https://ghp_xxx@github.com/...)。"""
    if looks_like_credential(url):
        return True
    if "://" not in url:
        return False
    parts = urlsplit(url)
    return parts.password is not None or (parts.username is not None and looks_like_credential(parts.username))


def _skipped(name: str) -> bool:
    return name in SKIPPED_DIRS or name.endswith(SKIPPED_SUFFIXES)


def _walk(root: Path) -> list[Path]:
    """root 下的文件与符号链接；root 本身是文件(或符号链接)时只有它；不存在时为空。跳过的目录不进入，不先遍历再过滤。
    指向目录的符号链接(os.walk 把它列在目录里)记为链接本身、不进入：改了链接指向能发现，指向的目录里的内容不算。"""
    if root.is_symlink() or root.is_file():
        return [root]
    if not root.is_dir():
        return []
    found: list[Path] = []
    for directory, directories, names in os.walk(root, followlinks=False):
        found += [Path(directory) / name for name in directories if (Path(directory) / name).is_symlink()]
        directories[:] = [name for name in directories if not _skipped(name)]
        found += [Path(directory) / name for name in names]
    return found


def _file_state(path: Path, previous: FileState | None) -> FileState | None:
    """只记普通文件与符号链接；取状态与读取之间被删的不计入。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        return None
    if previous is not None and (previous.size, previous.mtime_ns) == (info.st_size, info.st_mtime_ns):
        return previous
    digest = _file_digest(path)
    if digest == "missing":
        return None
    return FileState(info.st_size, info.st_mtime_ns, digest)
