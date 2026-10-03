"""修复报告的数据(architecture/07 4.12、第 5 章)：交接文档 outputs、改动的补丁与修复评分。

评分：代码项由 evaluation.scoring.score_output 按修复评分表计算(改动取本次补丁、规则取项目配置)；judge 项由本次
评审输出的 items 给出，评审没有给出的记为 unknown。结果写 scores，stage 为 fix、对象为 Issue 编号。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from tightrein.domain.clock import Clock
from tightrein.domain.enums import ScoreMethod, ScoreResult, Stage
from tightrein.evaluation import rubric
from tightrein.evaluation.scorers.base import ItemResult, ScoringContext
from tightrein.evaluation.scoring import score_output
from tightrein.guards.policy import GuardSettings
from tightrein.store.repos.scores import ScoreRecord

NOT_REVIEWED = "评审没有给出该项"
DIFF_HEADER = "diff --git a/"


class PatchReader(Protocol):
    def patch(self, repo: Path, base: str) -> str: ...

    def untracked(self, repo: Path, include_ignored: bool = False) -> tuple[str, ...]: ...


def patch_text(git: PatchReader, worktree: Path, base: str) -> str:
    """相对 base 的改动，未跟踪的新文件以新增文件的形式附在后面。"""
    parts = [git.patch(worktree, base)]
    for path in git.untracked(worktree):
        target = worktree / path
        if target.is_symlink() or not target.is_file():
            continue
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        parts.append(f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
                     f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines))
    return "".join(part if part.endswith("\n") or not part else part + "\n" for part in parts)


def without_files(patch: str, paths: Collection[str]) -> str:
    """去掉补丁中这些文件的部分(评分时不计本 Issue 的复现测试)。"""
    if not paths:
        return patch
    kept: list[str] = []
    skipping = False
    for line in patch.splitlines(keepends=True):
        if line.startswith(DIFF_HEADER):
            skipping = line.rstrip("\n").rsplit(" b/", 1)[-1] in paths
        if not skipping:
            kept.append(line)
    return "".join(kept)


def judged(results: Sequence[ItemResult], review_items: Sequence[Mapping[str, Any]]) -> list[ItemResult]:
    """用评审的结论替换 judge 项；不适用的保持不变。"""
    given = {item["itemId"]: item for item in review_items}
    merged = []
    for result in results:
        if result.method is not ScoreMethod.JUDGE or result.result is ScoreResult.NOT_APPLICABLE:
            merged.append(result)
            continue
        item = given.get(result.item_id)
        merged.append(ItemResult(result.item_id, ScoreMethod.JUDGE, ScoreResult(item["result"]), item["reason"])
                      if item is not None else ItemResult(result.item_id, ScoreMethod.JUDGE, ScoreResult.UNKNOWN,
                                                          NOT_REVIEWED))
    return merged


def score(handoff: Mapping[str, Any], patch: str, settings: GuardSettings, review_items: Sequence[Mapping[str, Any]],
          clock: Clock) -> list[ItemResult]:
    results = score_output(Stage.FIX, handoff, ScoringContext(diff_text=patch, guard_settings=settings), None, clock,
                           rubric.load(Stage.FIX))
    return judged(results, review_items)


def score_records(results: Sequence[ItemResult], run_id: str, issue_id: str, attempt: int,
                  clock: Clock) -> list[ScoreRecord]:
    return [ScoreRecord(Stage.FIX, run_id, issue_id, attempt, item.item_id, item.result, item.method, clock.now(),
                        item.reason) for item in results]
