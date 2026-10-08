"""同步 main：把最新的 origin/main 合并进修复分支(merge --no-ff，不用 rebase、不改写已推送的历史)。

- 判断时 fetch，合并的正是判断时看到的 origin/main；main 没有新提交就什么都不做；
- 先算「main 新提交改到的文件」与修复文件的交集，main 一侧从 merge-base 算(直接拿 HEAD 比 origin/main，修复自己的
  文件总会被算进去)。交集不为空或解决过冲突时，审查过的改动已经变了，退回实施重新审查；交集为空时改动哈希与审查时
  相同(protocol/git 的 review_base、diff_hash)，接着推送，由 CI 验证；
- 做决定时(fetch、确认工作区干净之后)记下分支、HEAD 与 origin/main(Git.state)，作为 expected 交给合并；用户解决冲突后
  完成合并提交时记下分支与 HEAD。执行前任何一项变了即抛 Stale、不执行(由 release.py 停到下次重新观察)；
- 冲突不自行取舍、不改冲突文件：合并停在进行中，写冲突报告(逐文件列两侧相关提交与冲突片段)，冲突文件清单另存
  (用户 git add 后它们不再显示为冲突)；用户解决后再次发布时先检查是否还留冲突标记，记下每个文件取了哪一侧，
  再完成合并提交。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tightrein.protocol.git import Git, MergeConflict
from tightrein.protocol.naming import FileName
from tightrein.protocol.runtime import Runtime
from tightrein.release.record import POINT_PR, Delivery, ReleaseBlocked, ReleaseState
from tightrein.store.files.atomic import write_text

MARKERS = ("<<<<<<< ", ">>>>>>> ")
SEPARATOR = "======="
FIX_SIDE, MAIN_SIDE, BOTH = "fix-side", "main-side", "both"
DECISION_KEYS = ("branch", "head", "originMain")  # 合并 main 时复核的前置条件
FINISH_KEYS = ("branch", "head")  # 完成合并提交时复核的前置条件
MUTUAL = "两侧都修改了这一段；如果两侧实现的是同一功能，属于互斥实现，需要人选择保留哪一侧"


@dataclass(frozen=True)
class SyncResult:
    main: str  # 判断时的 origin/main
    merged: str | None  # 合并后的 HEAD；main 没有新提交时为 None
    overlap: tuple[str, ...] = ()  # main 新改到的文件与修复文件的交集
    resolutions: dict[str, str] = field(default_factory=dict)  # 解决过冲突的文件 → 取了哪一侧

    @property
    def needs_review(self) -> bool:
        return bool(self.overlap or self.resolutions)


@dataclass(frozen=True)
class ConflictFile:
    path: str
    fix_commits: str
    main_commits: str
    hunks: tuple[tuple[str, str], ...]


def sync(runtime: Runtime, issue: str, delivery: Delivery, state: ReleaseState, worktree: Git) -> SyncResult:
    """合并最新的 origin/main。冲突时写报告并抛 ReleaseBlocked；用户解决冲突后再次调用即完成合并。"""
    if worktree.merge_head() is not None:
        return _finish_merge(runtime, issue, state, worktree)
    worktree.fetch()
    main_ref = f"origin/{worktree.main_branch}"
    main = worktree.rev_parse(main_ref)
    if worktree.is_ancestor(main, "HEAD"):
        return SyncResult(main, None)
    status = worktree.status()
    if not status.clean:
        raise ReleaseBlocked(POINT_PR, f"工作区不干净，不能同步 main：{'、'.join(status.changed_paths)}")
    base = worktree.merge_base("HEAD", main)
    overlap = overlapping(delivery.changed_files, [change.path for change in worktree.numstat(base, main)])
    decided = worktree.state(DECISION_KEYS)
    try:
        merged = worktree.merge(main, scope=runtime.scope(issue, POINT_PR), expected=decided)
    except MergeConflict as error:
        state.conflicts = list(error.files)
        report = write_conflicts(runtime, issue, worktree, error.files)
        raise ReleaseBlocked(POINT_PR, f"合并 {main_ref} 出现冲突：{'、'.join(error.files)}；冲突报告 {report}",
                             options=("在修复目录解决冲突并 git add 后重新发布", "放弃这次合并(git merge --abort)后重新发布"),
                             command=f"tightrein run --object {issue}") from error
    return SyncResult(main, merged, overlap)


def overlapping(fix_files: Sequence[str], main_files: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(set(fix_files) & set(main_files)))


def markers_left(worktree: Path, files: Sequence[str]) -> list[str]:
    left = []
    for path in files:
        target = worktree / path
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines() if target.is_file() else []
        if any(line.startswith(MARKERS) for line in lines):
            left.append(path)
    return left


def hunks(text: str) -> list[tuple[str, str]]:
    """冲突标记之间两侧的片段：(修复侧，main 侧)。"""
    found: list[tuple[str, str]] = []
    ours: list[str] = []
    theirs: list[str] = []
    side = None
    for line in text.splitlines():
        if line.startswith(MARKERS[0]):
            side, ours, theirs = "ours", [], []
        elif line.startswith(SEPARATOR) and side == "ours":
            side = "theirs"
        elif line.startswith(MARKERS[1]) and side == "theirs":
            found.append(("\n".join(ours), "\n".join(theirs)))
            side = None
        elif side == "ours":
            ours.append(line)
        elif side == "theirs":
            theirs.append(line)
    return found


def resolution(fix_side: str | None, main_side: str | None, resolved: str) -> str:
    """解决结果与修复侧一致、与 main 侧一致，还是两侧合并。"""
    if resolved == fix_side:
        return FIX_SIDE
    if resolved == main_side:
        return MAIN_SIDE
    return BOTH


def render_conflicts(issue: str, files: Sequence[ConflictFile]) -> str:
    parts = [f"# Issue {issue} 合并 origin/main 的冲突", "",
             ("tightrein 不自行选择任何一侧，也不修改冲突文件。在修复目录逐个文件解决并 git add 后重新发布；"
              "也可以 git merge --abort 放弃这次合并。")]
    for item in files:
        parts += ["", f"## {item.path}", "", "修复分支一侧的提交：", "", f"```\n{item.fix_commits.strip() or '无'}\n```",
                  "", "origin/main 一侧的提交：", "", f"```\n{item.main_commits.strip() or '无'}\n```"]
        for number, (ours, theirs) in enumerate(item.hunks, start=1):
            parts += ["", f"### 冲突片段 {number}", "", MUTUAL, "", "修复分支：", "", f"```\n{ours}\n```", "",
                      "origin/main：", "", f"```\n{theirs}\n```"]
    return "\n".join(parts).rstrip() + "\n"


def write_conflicts(runtime: Runtime, issue: str, worktree: Git, files: Sequence[str]) -> Path:
    found = [ConflictFile(path, _side_log(worktree, "MERGE_HEAD..HEAD", path),
                          _side_log(worktree, "HEAD..MERGE_HEAD", path),
                          tuple(hunks((worktree.repo / path).read_text(encoding="utf-8", errors="replace"))))
             for path in files]
    path = runtime.workspace.step_file(issue, FileName(POINT_PR, "evidence", "md"))
    write_text(path, render_conflicts(issue, found))
    return path


def _finish_merge(runtime: Runtime, issue: str, state: ReleaseState, worktree: Git) -> SyncResult:
    """用户解决冲突之后：还有冲突或冲突标记就继续等；否则记下每个文件取了哪一侧，完成合并提交。"""
    status = worktree.status()
    files = sorted(set(status.unmerged) | set(state.conflicts))
    left = sorted(set(status.unmerged) | set(markers_left(worktree.repo, files)))
    if left:
        raise ReleaseBlocked(POINT_PR, f"合并 origin/main 还有没解决的冲突：{'、'.join(left)}",
                             command=f"tightrein run --object {issue}")
    main = worktree.merge_head() or ""
    resolutions = {path: resolution(worktree.show("HEAD", path), worktree.show("MERGE_HEAD", path),
                                    (worktree.repo / path).read_text(encoding="utf-8", errors="replace"))
                   for path in files}
    message = f"Merge origin/{worktree.main_branch} into {worktree.head().branch}\n"
    base = worktree.merge_base("HEAD", main)
    decided = worktree.state(FINISH_KEYS, base=base)
    merged = worktree.commit(status.changed_paths or files, message, base=base, scope=runtime.scope(issue, POINT_PR),
                             expected=decided)
    state.conflicts = []
    return SyncResult(main, merged, (), resolutions)


def _side_log(worktree: Git, rev_range: str, path: str) -> str:
    return "\n".join(f"{item.commit[:12]} {item.subject}" for item in worktree.log(rev_range, [path]))
