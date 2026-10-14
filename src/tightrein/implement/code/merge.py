"""编码轮里合并 main：发布合并 origin/main 冲突后退回编码(release/release.py 的 _back_to_code)，由这一轮解决。

- 发布遇到冲突时已放弃那次合并，把要合并的 main、冲突文件与 main 一侧的相关提交记在 release.pr 的交接里
  (implement/context 的 returned)；这一轮开始前由程序合并那个 main(merge --no-ff，不 rebase)，冲突留在文件里；
- agent 只改文件解决冲突，不做 git 写操作；冲突说明放在修正说明最前面：保留两侧的意图，两侧是同一功能的互斥实现、
  无法兼顾时中止，交给用户选择(code.py 把这种中止归为需要用户)；
- agent 返回后程序核对：还有没解决的冲突或冲突标记，或这一轮没有完成，就放弃合并(下一轮重来)；都解决了由程序完成
  合并提交(提交信息与发布阶段人工解决后完成的合并相同)；
- 这一轮改动的比较基准是合并进来的 main：合并完成后 review_base 就是它，自检、审查、交付照常相对它算改动。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tightrein.implement.context import BACK_TO
from tightrein.protocol.git import Git, MergeConflict
from tightrein.release.sync import markers_left

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime

POINT = "implement.code"


@dataclass(frozen=True)
class Sync:
    main: str  # 要合并进来的 main(发布时 fetch 到的 origin/main)
    commits: tuple[str, ...]  # main 一侧改到冲突文件的提交(「短哈希 标题」)


def pending(context: ImplementContext, git: Git) -> Sync | None:
    """发布退回编码解决冲突、而那个 main 还没合并进来时要合并的内容；不需要时为 None。"""
    returned = context.returned
    if returned is None or returned.facts.get(BACK_TO) != POINT or not returned.facts.get("main"):
        return None
    main = str(returned.facts["main"])
    if git.is_ancestor(main, "HEAD"):
        return None  # 前一轮已合并好(之后是自检或审查交回的修正)
    return Sync(main, tuple(str(item) for item in returned.facts.get("mainCommits") or []))


def start(runtime: Runtime, issue: str, git: Git, sync: Sync) -> tuple[str, ...]:
    """开始合并，返回冲突文件；没有冲突时合并已完成，为空。"""
    try:
        git.merge(sync.main, scope=runtime.scope(issue, POINT))
    except MergeConflict as error:
        return tuple(error.files)
    return ()


def note(git: Git, sync: Sync, conflicts: Sequence[str]) -> str:
    """交给编码的冲突说明(放在修正说明最前面)。"""
    main = f"origin/{git.main_branch}({sync.main[:12]})"
    commits = "；".join(sync.commits) or "无"
    if not conflicts:
        return (f"发布前合并最新的 {main} 时与本修复冲突，程序在这一轮重新合并时已自动合并完成。先看合并进来的改动是否"
                f"影响本修复，跑受影响的检查，有问题按方案修正。main 一侧相关的提交：{commits}")
    return (f"发布前合并最新的 {main} 与本修复冲突，程序已开始这次合并，下面的文件里留有冲突标记：{'、'.join(conflicts)}。"
            f"main 一侧相关的提交：{commits}(可以用只读的 git log、git show、git diff 查看)。这一轮先逐个文件解决冲突："
            "保留两侧的意图，两侧各自新增的测试、函数、配置项都留下，按需要调整顺序或合并写法，不丢掉任何一侧的改动；"
            "去掉全部冲突标记后跑受影响的检查。不执行任何 git 写操作(不 add、不 commit、不 merge --abort)，合并提交由程序"
            "完成。两侧是同一功能的互斥实现、无法兼顾时不要自行取舍：status 写 aborted，在 bigIssue 中写明两侧各自的做法"
            "与冲突位置，交给用户选择保留哪一侧。")


def unresolved(git: Git, conflicts: Sequence[str]) -> list[str]:
    """还留冲突标记的冲突文件(agent 不 git add，暂存区里它们仍是未合并状态，只能看内容)。"""
    return markers_left(git.repo, conflicts)


def finish(runtime: Runtime, issue: str, git: Git, conflicts: Sequence[str]) -> str:
    """完成合并提交，返回新的 HEAD。只暂存冲突文件(其余合并结果已在暂存区)；agent 在别处的改动照常留在工作区，
    由发布阶段与修复一起提交。"""
    head = git.head()
    message = f"Merge origin/{git.main_branch} into {head.branch}\n"
    base = git.merge_base("HEAD", "MERGE_HEAD")
    return git.commit(list(conflicts), message, base=base, scope=runtime.scope(issue, POINT))
