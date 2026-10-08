"""gh 的只读查询与写操作(protocol/git.md)：PR 查找、创建与更新、检查、合并、评论，以及 GitHub Issue 同步所需的读写。

- 与 git 一样经 protocol/process.py 以参数数组启动；所有命令都带 `--repo <owner/name>`，不依赖工作目录推断仓库；
- 正文(PR 描述、评论、Issue 正文)一律以 `--body-file -` 从标准输入传入：不经命令行转义，也不出现在进程列表里；
- 退出码 4 为未登录(AuthError)，超时为网络错误，其余非零为 CommandFailed；只读查询网络错误时重试，写操作不重试；
- 写操作经 run_once 记幂等键，状态不明时先到 GitHub 上对账(该分支是否已有打开的 PR、带标记的 Issue 是否已建)。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.protocol.git.git import (
    AuthError,
    CommandFailed,
    GitError,
    WriteScope,
    failure,
    launch,
    once,
    retried,
)
from tightrein.protocol.naming import parse_iso
from tightrein.protocol.process import Command, Outcome, ProcessRunner
from tightrein.settings.load import Settings

if TYPE_CHECKING:
    from tightrein.protocol.security import Redactor

GH = "gh"
AUTH_EXIT = 4
STDIN = "-"
OPEN = "OPEN"
MERGED = "MERGED"
PR_FIELDS = "number,url,state,headRefName,headRefOid,mergedAt,mergeCommit,closedAt,reviewDecision"
MERGE_FIELDS = "state,isDraft,mergeable,mergeStateStatus,headRefOid,reviews"
CHECK_FIELDS = "name,state,bucket"
REQUIRED_STATUS_CHECKS = "required_status_checks"
# 列出 Issue 与标签时一次取的条数：镜像 Issue 按标记找回、读开关状态都要看全
LIST_LIMIT = 1000
COMPLETED = "completed"
NOT_PLANNED = "not planned"
SEPARATOR = "\0"  # 幂等键内容中隔开「加」与「减」两组标签
FIXED_ENV: Mapping[str, str] = {"GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"}
# 免费账号的私有仓库没有规则集功能，接口返回 403；gh 只在错误输出末尾给出状态码
_FORBIDDEN = re.compile(r"\(HTTP 403\)\s*$")
_REMOTE = re.compile(r"github\.com[:/]+(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_ISSUE_URL = re.compile(r"https://\S+/issues/(?P<number>\d+)")
_PULL_URL = re.compile(r"https://\S+/pull/(?P<number>\d+)")


class PublicRepository(GitError):
    """公开仓库不建镜像 Issue：Issue 正文含内部错误与代码位置。"""


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str  # OPEN、CLOSED、MERGED
    head_ref: str | None = None
    head: str | None = None
    merged_at: datetime | None = None
    merge_commit: str | None = None
    closed_at: datetime | None = None
    review_decision: str | None = None


@dataclass(frozen=True)
class MergeFacts:
    """判断能否自动合并所需的 PR 字段。"""

    state: str
    draft: bool
    mergeable: str | None
    merge_state: str | None
    head: str | None
    reviews: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class Check:
    """必需检查的一项；bucket 为 pass、fail、cancel、pending、skipping。"""

    name: str
    state: str
    bucket: str


@dataclass(frozen=True)
class IssueRef:
    number: int
    url: str


@dataclass(frozen=True)
class RemoteIssue:
    number: int
    state: str  # open、closed
    reason: str | None = None  # gh 的 stateReason：COMPLETED、NOT_PLANNED、DUPLICATE、REOPENED


@dataclass(frozen=True)
class RepositoryFacts:
    private: bool
    issues_enabled: bool


@dataclass(frozen=True)
class NewIssue:
    title: str
    body: str
    marker: str  # 正文中的本地标记 `<!-- tightrein:<项目>:<编号> -->`，中断后据此在远端找回
    labels: tuple[str, ...] = ()
    parent: int | None = None
    blocked_by: int | None = None


def repo_slug(url: str) -> str | None:
    """GitHub 远程地址(https、ssh、scp 形式)中的 owner/name；不是 GitHub 地址时为 None。"""
    match = _REMOTE.search(url.strip())
    return match.group("slug") if match else None


class GitHub:
    def __init__(self, cwd: Path, slug: str, runner: ProcessRunner, env: Mapping[str, str], settings: Settings, *,
                 redactor: Redactor | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        """cwd 只要求存在(命令都带 --repo)；slug 为 owner/name。"""
        self.cwd = cwd
        self.slug = slug
        self.runner = runner
        self.env = {**env, **FIXED_ENV}
        self.redactor = redactor
        self.sleep = sleep
        self.program = settings.get("tools.gh.path") or GH
        self.timeout_s = settings.duration("limits.timeouts.command")
        self.settings = settings
        self.retries = settings.get("limits.retry.attempts")

    # PR：只读

    def pr_for_branch(self, branch: str) -> PullRequest | None:
        """该分支的 PR：有打开的取打开的，否则取编号最大的一个；没有时为 None。"""
        items = self._json("pr", "list", "--head", branch, "--state", "all", "--json", "number,url,state")
        pulls = sorted((_pull(item) for item in items or []), key=lambda pull: (pull.state == OPEN, pull.number))
        return pulls[-1] if pulls else None

    def pull_state(self, branch: str) -> dict[str, str | None]:
        """做决定时观察的前置条件(create_pr 的 expected)：该分支上打开的 PR 编号，没有时为 None。"""
        pull = self.pr_for_branch(branch)
        return {"pullRequest": str(pull.number) if pull is not None and pull.state == OPEN else None}

    def pr_for_commit(self, commit: str) -> PullRequest | None:
        items = self._json("pr", "list", "--search", commit, "--state", "merged", "--json", "number,url,state,mergedAt")
        return _pull(items[0]) if items else None

    def pr_view(self, number: int) -> PullRequest:
        return _pull(self._json("pr", "view", str(number), "--json", PR_FIELDS))

    def pr_body(self, number: int) -> str:
        return str(self._json("pr", "view", str(number), "--json", "body")["body"])

    def merge_facts(self, number: int) -> MergeFacts:
        item = self._json("pr", "view", str(number), "--json", MERGE_FIELDS)
        return MergeFacts(state=item["state"], draft=bool(item.get("isDraft")), mergeable=item.get("mergeable") or None,
                          merge_state=item.get("mergeStateStatus") or None, head=item.get("headRefOid") or None,
                          reviews=tuple(item.get("reviews") or ()))

    def pr_checks(self, number: int) -> list[Check]:
        """必需检查；`--json` 时退出码不反映检查结果，由调用方按 bucket 判断。"""
        found = self._run("pr", "checks", str(number), "--required", "--json", CHECK_FIELDS, ok=None)
        if found.exit_code != 0 and not found.stdout.strip():
            raise failure(CommandFailed, (self.program, "pr", "checks", str(number)), found, self._redact)
        return [Check(item["name"], item["state"], item["bucket"]) for item in json.loads(found.stdout or "[]")]

    def required_checks(self, branch: str) -> bool:
        """主分支是否要求必需检查：分支保护或规则集。"""
        found = self._json("api", f"repos/{self.slug}/branches/{branch}", repo=False) or {}
        checks = (found.get("protection") or {}).get("required_status_checks") or {}
        if found.get("protected") and (checks.get("contexts") or checks.get("checks")):
            return True
        try:
            rules = self._json("api", f"repos/{self.slug}/rules/branches/{branch}", repo=False)
        except CommandFailed as error:
            if _FORBIDDEN.search(error.stderr):
                return False
            raise
        return any(rule.get("type") == REQUIRED_STATUS_CHECKS for rule in rules or [])

    # PR：写

    def create_pr(self, branch: str, base: str, title: str, body: str, *, scope: WriteScope,
                  expected: Mapping[str, str | None] | None = None) -> int:
        """先查后做：该分支已有打开的 PR 时只在描述有变化时更新，否则新建。返回 PR 编号。
        expected 为做决定时的 pull_state(branch)：要新建时再观察一次，不同(如当时有 PR、现在被关了)即抛 Stale。"""
        existing = self.pr_for_branch(branch)
        if existing is not None and existing.state == OPEN:
            return self.update_pr(existing.number, body, scope=scope)

        def opened() -> int | None:
            pull = self.pr_for_branch(branch)
            return pull.number if pull is not None and pull.state == OPEN else None

        def action() -> int:
            found = self._write("pr", "create", "--base", base, "--head", branch, "--title", title,
                                "--body-file", STDIN, body=body)
            return int(_last(_PULL_URL, found.stdout, "gh pr create").group("number"))

        return once(scope, "pr-create", (branch, base, title, body), action, opened, expected=expected,
                    observe=lambda keys: self.pull_state(branch))

    def update_pr(self, number: int, body: str, *, scope: WriteScope) -> int:
        """只在描述有变化时 `gh pr edit`，没变化什么都不做。"""
        if self.pr_body(number).strip() == body.strip():
            return number

        def updated() -> int | None:
            return number if self.pr_body(number).strip() == body.strip() else None

        def action() -> int:
            self._write("pr", "edit", str(number), "--body-file", STDIN, body=body)
            return number

        return once(scope, "pr-edit", (str(number), body), action, updated)

    def merge_pr(self, number: int, head: str, method: str, *, auto: bool = False, scope: WriteScope) -> int:
        """`--match-head-commit <判断时的 head>`：判断后有人再推送就合并失败，不会合进未审查的提交。带 --repo 时 gh 只删
        远程分支，本地分支与 worktree 由清理负责。auto 为真时开启 GitHub 原生自动合并(必需检查通过后由平台合并)。"""
        def merged() -> int | None:
            return number if self.pr_view(number).state == MERGED else None

        def action() -> int:
            self._write("pr", "merge", str(number), f"--{method}", *(("--auto",) if auto else ()), "--delete-branch",
                        "--match-head-commit", head)
            return number

        return once(scope, "pr-merge-auto" if auto else "pr-merge", (str(number), head, method), action, merged)

    def comment(self, number: int, body: str, *, scope: WriteScope) -> str:
        """在 PR 上发一条评论；同一对象、步骤、正文只发一次。"""
        return self._comment("pr", number, body, scope)

    # Issue 同步

    def repository(self) -> RepositoryFacts:
        item = self._json("repo", "view", self.slug, "--json", "isPrivate,hasIssuesEnabled", repo=False)
        return RepositoryFacts(bool(item["isPrivate"]), bool(item["hasIssuesEnabled"]))

    def labels(self) -> set[str]:
        return {item["name"] for item in self._json("label", "list", "--json", "name", "--limit", str(LIST_LIMIT))}

    def issue_states(self) -> dict[int, RemoteIssue]:
        items = self._json("issue", "list", "--state", "all", "--json", "number,state,stateReason",
                           "--limit", str(LIST_LIMIT))
        return {item["number"]: RemoteIssue(item["number"], item["state"].lower(), item.get("stateReason") or None)
                for item in items}

    def find_marker(self, marker: str) -> IssueRef | None:
        """正文含有本地标记的 Issue；有多个时取编号最小的。"""
        items = self._json("issue", "list", "--state", "all", "--json", "number,url,body", "--limit", str(LIST_LIMIT))
        found = sorted((item["number"], item["url"]) for item in items if marker in (item.get("body") or ""))
        return IssueRef(*found[0]) if found else None

    def issue_create(self, issue: NewIssue, *, scope: WriteScope) -> IssueRef:
        """建镜像 Issue：公开仓库拒绝；正文带本地标记，中断后按标记在远端找回，不重复建。parent、blocked_by 为 GitHub
        原生的子 Issue 与阻塞关系(gh 2.94 起支持)。"""
        if not self.repository().private:
            raise PublicRepository(f"{self.slug} 是公开仓库，不建镜像 Issue(正文含内部错误与代码位置)")
        body = issue.body if issue.marker in issue.body else f"{issue.body.rstrip()}\n\n{issue.marker}\n"
        relations = [*(("--parent", str(issue.parent)) if issue.parent is not None else ()),
                     *(("--blocked-by", str(issue.blocked_by)) if issue.blocked_by is not None else ())]

        def action() -> dict[str, Any]:
            found = self._write("issue", "create", "--title", issue.title, "--body-file", STDIN,
                                *(part for label in issue.labels for part in ("--label", label)), *relations,
                                body=body)
            match = _last(_ISSUE_URL, found.stdout, "gh issue create")
            return {"number": int(match.group("number")), "url": match.group(0)}

        def recovered() -> dict[str, Any] | None:
            ref = self.find_marker(issue.marker)
            return None if ref is None else {"number": ref.number, "url": ref.url}

        result = once(scope, "issue-create", (issue.marker,), action, recovered)
        return IssueRef(result["number"], result["url"])

    def issue_edit(self, number: int, title: str, body: str, *, scope: WriteScope) -> int:
        def action() -> int:
            self._write("issue", "edit", str(number), "--title", title, "--body-file", STDIN, body=body)
            return number

        return once(scope, "issue-edit", (str(number), title, body), action, lambda: None)

    def issue_labels(self, number: int, add: Sequence[str], remove: Sequence[str], *, scope: WriteScope) -> int:
        """只移除仓库中存在的旧标签：gh 移除不存在的标签会整条命令失败。"""
        existing = self.labels() if remove else set()
        removable = [label for label in remove if label in existing]
        if not add and not removable:
            return number
        args = ["issue", "edit", str(number)]
        if add:
            args += ["--add-label", ",".join(add)]
        if removable:
            args += ["--remove-label", ",".join(removable)]

        def action() -> int:
            self._write(*args)
            return number

        return once(scope, "issue-labels", (str(number), *add, SEPARATOR, *removable), action, lambda: None)

    def issue_comment(self, number: int, body: str, *, scope: WriteScope) -> str:
        return self._comment("issue", number, body, scope)

    def issue_close(self, number: int, reason: str, *, scope: WriteScope) -> int:
        """reason 为 completed 或 not planned。"""
        def action() -> int:
            self._write("issue", "close", str(number), "--reason", reason)
            return number

        return once(scope, "issue-close", (str(number), reason), action, lambda: self._state_is(number, "closed"))

    def issue_reopen(self, number: int, *, scope: WriteScope) -> int:
        def action() -> int:
            self._write("issue", "reopen", str(number))
            return number

        return once(scope, "issue-reopen", (str(number),), action, lambda: self._state_is(number, "open"))

    def label_create(self, name: str, color: str, description: str, *, scope: WriteScope) -> str:
        def action() -> str:
            self._write("label", "create", name, "--color", color, "--description", description)
            return name

        return once(scope, "label-create", (name,), action, lambda: name if name in self.labels() else None)

    # 内部

    def _comment(self, kind: str, number: int, body: str, scope: WriteScope) -> str:
        def posted() -> str | None:
            item = self._json(kind, "view", str(number), "--json", "comments")
            same = [comment.get("url", "") for comment in item.get("comments") or []
                    if (comment.get("body") or "").strip() == body.strip()]
            return same[-1] if same else None

        def action() -> str:
            return self._write(kind, "comment", str(number), "--body-file", STDIN, body=body).stdout.strip()

        return once(scope, f"{kind}-comment", (str(number), body), action, posted)

    def _state_is(self, number: int, state: str) -> int | None:
        item = self._json("issue", "view", str(number), "--json", "state")
        return number if str(item["state"]).lower() == state else None

    # 其余只读查询：PR 的评论与评审、规则集、`pr checks`(含 --watch)

    def query(self, *args: str, repo: bool = True, timeout_s: float | None = None, retry: bool = True) -> Outcome:
        """执行一条只读的 gh 命令，退出码交调用方判断(`gh pr checks` 的退出码不反映检查结果)。
        长时间等待的命令(--watch)传 retry=False：超时不该再整段重等。"""
        return self._run(*args, ok=None, read=retry, repo=repo, timeout_s=timeout_s)

    def query_json(self, *args: str, repo: bool = True) -> Any:
        """只读查询的 JSON 结果；非零退出即抛带类型的错误。"""
        return self._json(*args, repo=repo)

    def _redact(self, text: str) -> str:
        return self.redactor.text(text) if self.redactor is not None else text

    def _json(self, *args: str, repo: bool = True) -> Any:
        return json.loads(self._run(*args, repo=repo).stdout or "null")

    def _write(self, *args: str, body: str | None = None) -> Outcome:
        return self._run(*args, stdin=body, read=False)

    def _run(self, *args: str, stdin: str | None = None, ok: Sequence[int] | None = (0,), read: bool = True,
             repo: bool = True, timeout_s: float | None = None) -> Outcome:
        argv = (self.program, *args, *(("--repo", self.slug) if repo else ()))
        command = Command(argv=argv, cwd=self.cwd, env=self.env, stdin=stdin, timeout_s=timeout_s or self.timeout_s)

        def attempt() -> Outcome:
            found = launch(self.runner, command, remote=True, redact=self._redact)
            if ok is None or found.exit_code in ok:
                return found
            kind = AuthError if found.exit_code == AUTH_EXIT else CommandFailed
            raise failure(kind, argv, found, self._redact)

        return retried(attempt, retries=self.retries if read else 0, settings=self.settings, sleep=self.sleep)


def _last(pattern: re.Pattern[str], text: str, what: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if not matches:
        raise CommandFailed(f"{what} 的输出中没有链接：{text.strip()[:200]}")
    return matches[-1]


def _time(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


def _pull(item: Mapping[str, Any]) -> PullRequest:
    merge_commit = item.get("mergeCommit")
    return PullRequest(
        number=item["number"], url=item["url"], state=item["state"], head_ref=item.get("headRefName"),
        head=item.get("headRefOid"), merged_at=_time(item.get("mergedAt")),
        merge_commit=merge_commit.get("oid") if merge_commit else None, closed_at=_time(item.get("closedAt")),
        review_decision=item.get("reviewDecision") or None,
    )
