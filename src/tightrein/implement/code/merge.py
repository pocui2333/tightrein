"""编码轮里合并 main：发布合并 origin/main 冲突后退回编码(release/release.py 的 _back_to_code)，由这一轮解决。

- 发布遇到冲突时已放弃那次合并，把要合并的 main、冲突文件与 main 一侧的相关提交记在 release.pr 的交接里
  (implement/context 的 returned)；这一轮开始前由程序合并那个 main(merge --no-ff，不 rebase)；
- 冲突按暂存区里各文件占的阶段分两类(start)：两侧都有、文件里留了冲突标记的，由 agent 改文件解决；没有冲突标记的
  (一侧删了另一侧改了、改名、二进制文件等)改文件解决不了，取哪一侧都会丢掉另一侧的改动，交给用户选保留哪一侧
  (gate：approve --option 1 修复一侧、2 main 一侧)；用户选了之后，程序在合并时把这些文件写成那一侧(choose)，
  冲突标记照常交给 agent；
- agent 只改文件，不做 git 写操作；冲突说明放在修正说明最前面，只说真留了冲突标记的文件：保留两侧的意图，两侧是
  同一功能的互斥实现、无法兼顾时中止，交给用户选择(code.py 把这种中止归为需要用户)；
- agent 返回后程序核对：还有没解决的冲突标记，或这一轮没有完成，就放弃合并(worktree 连同分支回到合并前，下一轮
  重来)；都解决了由程序完成合并提交(提交信息与发布阶段人工解决后完成的合并相同)；
- 程序的合并与合并提交不记幂等键：它们会被放弃、会因中断随检查点退回，做没做以那个 main 是否已是 HEAD 的祖先为准
  (pending)，记了键再做时会被当成已做过而跳过；
- 这一轮改动的比较基准是合并进来的 main：合并完成后 review_base 就是它，自检、审查、交付照常相对它算改动；
- 合并完成后的轮次照抄上一轮交接的 mergedMain(carried)，直到下一次退回：审查从最近一次编码交接读用户为改文件解决
  不了的冲突选的一侧，与人在发布阶段的取舍一样作数，不把用户有意舍弃的 main 一侧报成丢掉。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from tightrein.implement.context import BACK_TO
from tightrein.protocol.git import Git, MergeConflict
from tightrein.release.sync import markers_left

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.runtime import Runtime

POINT = "implement.code"
APPROVE = "approve"
FIX, MAIN = "fix", "main"  # 用户选保留的一侧
SIDES = {1: FIX, 2: MAIN}  # 关卡上 approve --option 的序号
SIDE_NAMES = {FIX: "修复一侧", MAIN: "main 一侧"}
BASE, OURS, THEIRS = 1, 2, 3  # 暂存区阶段：共同祖先、修复分支、合并进来的 main
# 没有冲突标记的冲突按两侧有没有这个文件说明(暂存区里占的阶段)
KINDS = {
    frozenset({BASE, OURS, THEIRS}): "两侧都改了，git 不能逐行合并(二进制文件等)，文件里没有冲突标记",
    frozenset({OURS, THEIRS}): "两侧都新加了这个文件，git 不能逐行合并(二进制文件等)，文件里没有冲突标记",
    frozenset({BASE, OURS}): "修复改了这个文件，main 删了它(或改了名)",
    frozenset({BASE, THEIRS}): "main 改了这个文件，修复删了它(或改了名)",
    frozenset({OURS}): "只有修复一侧有这个文件(main 一侧删了或改了名)",
    frozenset({THEIRS}): "只有 main 一侧有这个文件(修复一侧删了或改了名)",
    frozenset({BASE}): "两侧都删了它或改成了不同的名字",
}
UNKNOWN_KIND = "git 无法用冲突标记表示的冲突"


@dataclass(frozen=True)
class Sync:
    main: str  # 要合并进来的 main(发布时 fetch 到的 origin/main)
    commits: tuple[str, ...]  # main 一侧改到冲突文件的提交(「短哈希 标题」)


@dataclass(frozen=True)
class Unmergeable:
    """没有冲突标记、改文件解决不了的冲突文件。"""

    path: str
    stages: frozenset[int]

    @property
    def kind(self) -> str:
        return KINDS.get(self.stages, UNKNOWN_KIND)

    def to_json(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind}


@dataclass(frozen=True)
class Merging:
    """这一轮合并 main 的冲突：没有冲突时合并已完成。"""

    conflicts: tuple[str, ...] = ()  # 留了冲突标记的文件：agent 改文件解决
    unmergeable: tuple[Unmergeable, ...] = ()  # 没有冲突标记的：用户选保留哪一侧
    side: str | None = None  # 已按用户的选择写好的一侧

    @property
    def conflicted(self) -> bool:
        """合并停在进行中，要解决后由程序完成合并提交。"""
        return bool(self.conflicts or self.unmergeable)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({*self.conflicts, *(item.path for item in self.unmergeable)}))

    def facts(self, sync: Sync) -> dict[str, Any]:
        """交接的 mergedMain；commit 在完成合并提交后补上。"""
        return {"main": sync.main, "conflicts": list(self.conflicts),
                "unmergeable": [item.to_json() for item in self.unmergeable], "side": self.side, "commit": None}


def pending(context: ImplementContext, git: Git) -> Sync | None:
    """发布退回编码解决冲突、而那个 main 还没合并进来时要合并的内容；不需要时为 None。"""
    returned = context.returned
    if returned is None or returned.facts.get(BACK_TO) != POINT or not returned.facts.get("main"):
        return None
    main = str(returned.facts["main"])
    if git.is_ancestor(main, "HEAD"):
        return None  # 前一轮已合并好(之后是自检或审查交回的修正)
    return Sync(main, tuple(str(item) for item in returned.facts.get("mainCommits") or []))


def carried(context: ImplementContext) -> dict[str, Any] | None:
    """前面的轮次已合并好的、这次退回要合并的 main(上一次编码交接的 mergedMain 记了合并提交)：那个 main 已在 HEAD
    里(pending 为 None)时由这一轮照抄进自己的交接，审查据此知道用户选的一侧。没有时为 None。"""
    returned, previous = context.returned, context.last(POINT)
    if returned is None or returned.facts.get(BACK_TO) != POINT or previous is None:
        return None
    merged = previous.facts.get("mergedMain") or {}
    if not merged.get("commit") or merged.get("main") != returned.facts.get("main"):
        return None
    return dict(merged)


def start(runtime: Runtime, issue: str, git: Git, sync: Sync) -> Merging:
    """开始合并，冲突按暂存区分类；没有冲突时合并已完成。不记幂等键(见模块说明)。"""
    try:
        git.merge(sync.main, scope=runtime.scope(issue, POINT), remember=False)
    except MergeConflict as error:
        return classify(git, error.files)
    return Merging()


def classify(git: Git, files: Sequence[str]) -> Merging:
    """两侧都有这个文件、文件里留了冲突标记的是改文件能解决的冲突；其余(一侧没有这个文件、二进制文件等)不是。"""
    stages = git.conflict_stages()
    marked = set(markers_left(git.repo, files))
    text = [path for path in files if {OURS, THEIRS} <= stages.get(path, frozenset()) and path in marked]
    others = [Unmergeable(path, stages.get(path, frozenset())) for path in files if path not in text]
    return Merging(tuple(text), tuple(others))


def chosen(context: ImplementContext, sync: Sync) -> str | None:
    """用户为这个 main 选的保留一侧：之前的轮次已按它合并过就沿用；上一次编码因改文件解决不了的冲突停下等用户，
    之后用户在编码关卡上 approve --option 1/2 就是这次的选择。只认编码关卡上的决定：用户在关卡上 reject 重出方案时，
    定案关卡上的 --option 是给需要拍板的点选的，不是选保留哪一侧。没有选(或 approve 没带序号)时为 None。"""
    previous = context.last(POINT)
    merged = (previous.facts.get("mergedMain") or {}) if previous is not None else {}
    if previous is None or merged.get("main") != sync.main:
        return None
    if merged.get("side") in SIDE_NAMES:
        return str(merged["side"])
    if not merged.get("unmergeable"):
        return None
    picked = [SIDES.get(item.option or 0) for item in context.decisions
              if item.at > (previous.created_at or "") and item.point == POINT and item.verdict == APPROVE]
    return picked[-1] if picked else None


def choose(git: Git, merging: Merging, side: str) -> Merging:
    """把改文件解决不了的冲突文件写成用户选的那一侧(不暂存，合并提交时一起暂存)；那一侧没有这个文件就删掉。"""
    stage = THEIRS if side == MAIN else OURS
    for item in merging.unmergeable:
        if stage in item.stages:
            git.keep_side(item.path, theirs=side == MAIN)
            continue
        target = git.repo / item.path
        if target.is_file() or target.is_symlink():
            target.unlink()
    return replace(merging, side=side)


def note(git: Git, sync: Sync, merging: Merging) -> str:
    """交给编码的冲突说明(放在修正说明最前面)：只说真留了冲突标记的文件。"""
    main = f"origin/{git.main_branch}({sync.main[:12]})"
    commits = "；".join(sync.commits) or "无"
    if not merging.conflicted:
        return (f"发布前合并最新的 {main} 时与本修复冲突，程序在这一轮重新合并时已自动合并完成。先看合并进来的改动是否"
                f"影响本修复，跑受影响的检查，有问题按方案修正。main 一侧相关的提交：{commits}")
    parts = [f"发布前合并最新的 {main} 与本修复冲突，程序已开始这次合并"]
    if merging.conflicts:
        parts.append(f"，下面的文件里留有冲突标记：{'、'.join(merging.conflicts)}")
    if merging.unmergeable and merging.side is not None:
        chosen_files = "；".join(f"{item.path}({item.kind})" for item in merging.unmergeable)
        parts.append(f"。下面的文件没有冲突标记、改文件解决不了，已按用户的选择写成{SIDE_NAMES[merging.side]}的版本"
                     f"(那一侧没有这个文件的已删除)，保持用户的取舍，按需要调整本修复的其他代码：{chosen_files}")
    parts.append(f"。main 一侧相关的提交：{commits}(可以用只读的 git log、git show、git diff 查看)。")
    if merging.conflicts:
        parts.append("这一轮先逐个文件解决冲突：保留两侧的意图，两侧各自新增的测试、函数、配置项都留下，按需要调整顺序或"
                     "合并写法，不丢掉任何一侧的改动；去掉全部冲突标记后跑受影响的检查。")
    else:
        parts.append("这一轮先看合并进来的改动与用户的取舍是否影响本修复，跑受影响的检查，有问题按方案修正。")
    parts.append("不执行任何 git 写操作(不 add、不 commit、不 merge --abort)，合并提交由程序完成。")
    if merging.conflicts:
        parts.append("两侧是同一功能的互斥实现、无法兼顾时不要自行取舍：status 写 aborted，在 bigIssue 中写明两侧各自的"
                     "做法与冲突位置，交给用户选择保留哪一侧。")
    return "".join(parts)


def gate(issue: str, sync: Sync, merging: Merging) -> dict[str, Any]:
    """改文件解决不了的冲突：请用户选这些文件保留哪一侧(implement.py 停在关卡时用它写待决定文档)。"""
    listed = "\n".join(f"- {item.path}：{item.kind}" for item in merging.unmergeable)
    marked = f"\n\n留了冲突标记的文件由编码解决：{'、'.join(merging.conflicts)}" if merging.conflicts else ""
    return {
        "decision": (f"合并 origin/main({sync.main[:12]}) 时，下面的文件有改文件解决不了的冲突(没有冲突标记)，"
                     f"要你选这些文件保留哪一侧：\n\n{listed}{marked}"),
        "options": [f"保留修复一侧(main 对这些文件的删除或改动不要)：tightrein approve {issue} --option 1",
                    (f"保留 main 一侧(修复对这些文件的改动不要，编码按合并后的代码调整修复)：tightrein approve {issue} "
                     "--option 2")],
        "recommendation": f"先看 main 一侧相关的提交再选：{'；'.join(sync.commits) or '无'}",
        "reason": "一侧删了另一侧改了、改名、二进制文件这类冲突，取哪一侧都会丢掉另一侧的改动，tightrein 不替你选",
        "ifNot": "Issue 停在这一步，不会自动继续",
        "command": f"tightrein approve {issue} --option <1|2>",
    }


def unresolved(git: Git, merging: Merging) -> list[str]:
    """还留冲突标记的冲突文件(agent 不 git add，暂存区里它们仍是未合并状态，只能看内容)。"""
    return markers_left(git.repo, merging.conflicts)


def finish(runtime: Runtime, issue: str, git: Git, merging: Merging) -> str:
    """完成合并提交，返回新的 HEAD。只暂存冲突文件(删掉的记为删除；其余合并结果已在暂存区)；agent 在别处的改动
    照常留在工作区，由发布阶段与修复一起提交。不记幂等键(见模块说明)。"""
    head = git.head()
    message = f"Merge origin/{git.main_branch} into {head.branch}\n"
    base = git.merge_base("HEAD", "MERGE_HEAD")
    return git.commit(list(merging.paths), message, base=base, scope=runtime.scope(issue, POINT), remember=False)
