"""准备(implement.prepare)：建修复分支与 worktree，跑准备命令与基准检查；另管 worktree 的检查点快照。

- 分支名按项目约定生成(protocol/git/format：项目写明 → 历史推断经确认 → 通用格式)；项目要求个人前缀时必须配置，
  且不能是 AI 或工具名称；分支名已存在(别人的，或本 Issue 上一次修复尝试留下的)时依次加 `-2`、`-3`…；
- worktree 经 protocol/git/worktrees 从 origin/<主分支> 新建；已存在就复用(中断后重来不重复建)；回归或重开后的又一次
  修复(assess/issue/attempts)用新的目录 `worktrees/<编号>_<次>`，不接着用上一次已合并的分支与目录；
- 检查点快照：每一步完成时把 worktree 的内容(含未跟踪的新文件)记成一个不挂在任何分支上的 commit，写进
  handoff 的 `worktreeCommit`；中断后把 worktree 恢复到上一个检查点，不续接半截状态。只写对象库与临时索引，
  不动 HEAD、分支与暂存区，改动照常由发布阶段提交。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from tightrein.assess.issue import attempts
from tightrein.implement.prepare import baseline
from tightrein.protocol.git import Git
from tightrein.protocol.git.format import BranchRejected, branch_name, branch_type, kebab, resolve
from tightrein.protocol.git.git import CommandFailed, failure, launch
from tightrein.protocol.git.worktrees import create_fix
from tightrein.protocol.handoff import Handoff, Metrics, Status
from tightrein.protocol.naming import FileName
from tightrein.protocol.process import Command
from tightrein.protocol.recovery import CHECKPOINT_COMMIT

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime
    from tightrein.store.tables.issues import Issue

POINT = "implement.prepare"
URGENT = "P0"  # 立即修的 P0 用 hotfix 分支
# 快照 commit 的作者：不是提交到分支上的 commit，不经 Git.commit，也就不涉及署名规则
SNAPSHOT_IDENTITY = {"GIT_AUTHOR_NAME": "tightrein", "GIT_AUTHOR_EMAIL": "tightrein@localhost",
                     "GIT_COMMITTER_NAME": "tightrein", "GIT_COMMITTER_EMAIL": "tightrein@localhost"}
SNAPSHOT_MESSAGE = "tightrein checkpoint\n"


def run(runtime: Runtime, context: ImplementContext) -> Handoff:
    issue = context.issue
    path = worktree_path(runtime, issue)
    try:
        branch = _branch(runtime, issue, path)
    except BranchRejected as error:
        return _handoff(runtime, issue, Status.FAILED, f"分支名不合规：{error}", {"reason": "branch_rejected"})
    if not path.is_dir():
        links = tuple(runtime.settings.section(POINT).get("links") or ())
        create_fix(runtime.git, path, branch, links=links, scope=runtime.scope(issue.id, POINT))
    tree = runtime.git.at(path)
    base = tree.head().commit or ""
    log = runtime.workspace.step_file(issue.id, FileName(POINT, "log", "log"))
    log.unlink(missing_ok=True)
    facts: dict[str, object] = {"branch": branch, "worktree": str(path), "baseCommit": base, CHECKPOINT_COMMIT: base,
                                "log": str(log)}
    prepared = baseline.prepare(runtime, path, log)
    facts["prepareCommands"] = [asdict(run) for run in prepared]
    broken = baseline.describe(prepared)
    if broken:
        return _handoff(runtime, issue, Status.FAILED, "准备命令失败，判为配置错误：" + "；".join(broken),
                        {**facts, "reason": "config_error"})
    checked = baseline.check(runtime, path, base, log)
    facts["baseChecks"] = checked.facts()
    if not checked.passed:
        return _handoff(runtime, issue, Status.FAILED,
                        "基准上的项目检查不通过，判为配置错误：" + "；".join(baseline.describe(checked.runs)),
                        {**facts, "reason": "config_error"})
    cached = "(基准检查取自缓存)" if checked.cached else ""
    return _handoff(runtime, issue, Status.PASSED, f"修复分支 {branch} 与 worktree 已就绪{cached}", facts,
                    passed=sum(run.result == baseline.PASSED for run in checked.runs))


def worktree_path(runtime: Runtime, issue: Issue) -> Path:
    """修复目录：第 1 次为 `worktrees/<编号>`，之后每次修复尝试另用 `worktrees/<编号>_<次>`。"""
    number = attempts.current(issue)
    return runtime.workspace.worktree(issue.id if number == 1 else f"{issue.id}_{number}")


def snapshot(git: Git) -> str:
    """worktree 当前内容的检查点 commit；干净时就是 HEAD，不另建对象。"""
    head = git.head().commit or ""
    if git.status().clean:
        return head
    env = {**git.env, **SNAPSHOT_IDENTITY}
    return _git(git, env, "commit-tree", _worktree_tree(git, head), "-p", head, stdin=SNAPSHOT_MESSAGE).strip()


def restore(git: Git, commit: str) -> None:
    """把 worktree 的内容恢复成检查点 commit 的样子：HEAD 不动，快照中的新文件恢复后仍为未跟踪。"""
    env = dict(git.env)
    _git(git, env, "clean", "-f", "-d", "--quiet")
    _git(git, env, "read-tree", "--reset", "-u", commit)
    _git(git, env, "read-tree", "HEAD")


def tree_of(git: Git, commit: str) -> str:
    return _git(git, dict(git.env), "rev-parse", f"{commit}^{{tree}}").strip()


def current_tree(git: Git) -> str:
    """worktree 当前内容的 tree(不建 commit)：与检查点的 tree 比较，判断有没有半截的改动。"""
    head = git.head().commit or ""
    return tree_of(git, head) if git.status().clean else _worktree_tree(git, head)


def _branch(runtime: Runtime, issue: Issue, path: Path) -> str:
    """已有 worktree 的分支 → Issue 记下的分支 → 按约定新生成(已被别人占用时加 -2)。"""
    if path.is_dir():
        found = runtime.git.at(path).head().branch
        if found:
            return found
    if issue.branch:
        return issue.branch
    conventions = resolve(runtime.settings, runtime.git.repo)
    kind = branch_type(issue.kind, urgent=issue.severity == URGENT, types=conventions.types)
    name = branch_name(conventions, kind=kind, issue=issue.id, slug=_slug(issue))
    number = 1
    found = name
    while runtime.git.branch_exists(found):
        number += 1
        found = f"{name}-{number}"
    return found


def _slug(issue: Issue) -> str:
    """分支名里的简称：评估写下的英文简称优先，其次标题中的英文词，都没有时用种类。"""
    return kebab(str(issue.extra.get("slug") or "")) or kebab(issue.title) or issue.kind


def _worktree_tree(git: Git, head: str) -> str:
    """用临时索引把 worktree 的全部内容(含未跟踪、不含被忽略的)写成 tree，不碰真正的暂存区。"""
    with tempfile.TemporaryDirectory() as directory:
        env = {**git.env, "GIT_INDEX_FILE": os.path.join(directory, "index")}
        _git(git, env, "read-tree", head)
        _git(git, env, "add", "--all")
        return _git(git, env, "write-tree").strip()


def _git(git: Git, env: Mapping[str, str], *args: str, stdin: str | None = None) -> str:
    """Git 类没有的底层命令(临时索引、commit-tree)：同样不经 shell、带 git 的固定环境与超时。"""
    redact = git.redactor.text if git.redactor is not None else (lambda text: text)
    command = Command(argv=("git", *args), cwd=git.repo, env=env, stdin=stdin, timeout_s=git.timeout_s)
    outcome = launch(git.runner, command, remote=False, redact=redact)
    if outcome.exit_code != 0:
        raise failure(CommandFailed, command.argv, outcome, redact)
    return outcome.stdout


def _handoff(runtime: Runtime, issue: Issue, status: Status, summary: str, facts: dict[str, object], *,
             passed: int | None = None) -> Handoff:
    return Handoff(point=POINT, subject=issue.id, run=runtime.run, status=status, summary=summary, facts=facts,
                   metrics=Metrics(calls=0, passed=passed))
