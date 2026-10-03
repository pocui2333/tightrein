"""评审(redesign/05-fix.md 第 8 步)：第 7 步的结果通过后按流程表运行 fix-reviewer：A 轻量，B 轻量、高风险另加深度
(深度评审须与写代码的模型不同家族，由 stages.fix.review.deep 配置)。输入含第 7 步的实际结果。

blockers 的 kind 须属于该模式允许的 ReviewFindingKind；缺少 `文件路径:行号`、位置在 worktree 中不存在、缺少触发条件
或类别不允许的问题丢弃，记入 discardedFindings，不计为不通过。没有保留的问题、也没有「无法判断」的评审项时通过。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.domain.enums import ReviewCategory, ReviewFindingKind, ReviewMode, RunnerStatus, ScoreResult
from tightrein.domain.fix import FixRisk
from tightrein.evaluation.scorers.code import location_problem
from tightrein.pipeline.fix.prompts import fix_reviewer
from tightrein.pipeline.fix.prompts.common import FixCalls
from tightrein.pipeline.fix.steps.checks import Finding
from tightrein.pipeline.fix.steps.context import FixContext

BASE_KINDS = frozenset({ReviewFindingKind.ROOT_CAUSE_UNFIXED, ReviewFindingKind.CALLER_BROKEN,
                        ReviewFindingKind.HARDCODE, ReviewFindingKind.NEW_ERROR_PATH,
                        ReviewFindingKind.REQUIREMENT_UNMET, ReviewFindingKind.REQUIREMENT_REDUCED})
ALLOWED_KINDS: dict[ReviewMode, frozenset[ReviewFindingKind]] = {
    ReviewMode.LIGHT: BASE_KINDS,
    ReviewMode.DEEP: BASE_KINDS | {ReviewFindingKind.AUTHZ, ReviewFindingKind.DATA_STRUCTURE,
                                   ReviewFindingKind.CONTRACT},
}


def _discard_reason(finding: Mapping[str, Any], worktree: Path, mode: ReviewMode) -> str | None:
    if finding["kind"] not in {kind.value for kind in ALLOWED_KINDS[mode]}:
        return f"问题类别 {finding['kind']} 不在{mode.label}允许的范围内"
    if not finding.get("location"):
        return "没有给出「文件路径:行号」"
    problem = location_problem(worktree, finding["location"])
    if problem is not None:
        return f"位置不存在：{problem}"
    if not (finding.get("trigger") or "").strip():
        return "没有给出触发条件"
    return None


def filter_findings(output: Mapping[str, Any], worktree: Path,
                    mode: ReviewMode) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    kept, discarded = [], []
    for finding in output["blockers"]:
        reason = _discard_reason(finding, worktree, mode)
        if reason is None:
            kept.append(finding)
        else:
            discarded.append({"finding": dict(finding), "reason": reason})
    return kept, discarded


@dataclass
class Review:
    mode: ReviewMode
    status: RunnerStatus
    output: Mapping[str, Any] | None = None
    kept: list[Mapping[str, Any]] = field(default_factory=list)
    discarded: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def unknown(self) -> list[Mapping[str, Any]]:
        return [item for item in (self.output or {}).get("items", []) if item["result"] == ScoreResult.UNKNOWN.value]

    @property
    def passed(self) -> bool:
        return self.status is RunnerStatus.OK and not self.kept and not self.unknown

    def findings(self) -> list[Finding]:
        return [Finding(f"review:{item['kind']}", item["location"], f"{item['problem']}(触发条件：{item['trigger']}；"
                        f"根源：{item['rootCause']})", ReviewCategory(item["category"])) for item in self.kept]


def review(calls: FixCalls, context: FixContext, plan: Mapping[str, Any], diff_text: str, notes: Sequence[str],
           mode: ReviewMode, risk: FixRisk, results: str, attempt: int) -> Review:
    result = calls.run(fix_reviewer.task(calls.prompt, context, plan, diff_text, notes, mode, attempt, risk=risk,
                                         results=results))
    if result.status is not RunnerStatus.OK or result.output is None:
        return Review(mode, result.status, error=result.error_type)
    kept, discarded = filter_findings(result.output, calls.prompt.workdir, mode)
    return Review(mode, result.status, result.output, kept, discarded)
