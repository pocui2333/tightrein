"""证据检查(architecture/06 4.6)：分诊评分表的各项全部由代码判定，取证之后不另设模型评审。

角色输出先整理成交接文档 outputs 的形状(判定、主张、证据、根因、缺少的信息、Issue 报告与评估)，再交给 evaluation 的
score_output，代码快照为已切换到取证 commit 的只读 worktree；生产与评测用的是同一份评分表与评分器。
评估(assessment)另由 assessment_problems 检查：判为成立或条件成立时必须给出；预估改动的已有文件在当前代码中存在
(新建的除外)；建议暂不修时须写重估条件。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import ScoreResult, Stage, Verdict, WorthRecommendation
from tightrein.evaluation.scorers.base import ItemResult, ScoringContext
from tightrein.evaluation.scoring import score_output
from tightrein.pipeline.triage.steps.claims import Claim

EVIDENCE_KEYS = ("facts", "trigger", "counterEvidence", "impact", "sourceOfPhenomenon")
CONFIRMING = frozenset({Verdict.CONFIRMED.value, Verdict.CONDITIONAL.value})


def outputs_for(claim: Claim, output: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "verdict": output["verdict"],
        "claim": claim.to_dict(),
        "evidence": {key: output[key] for key in EVIDENCE_KEYS},
        "rootCauses": list(output["rootCauses"]),
        "missingInfo": list(output["missingInfo"]),
        "report": output.get("report"),
        "assessment": output.get("assessment"),
    }


def assessment_problems(output: Mapping[str, Any], worktree: Path) -> list[str]:
    assessment = output.get("assessment")
    if assessment is None:
        return ["判为成立或条件成立时必须给出 assessment(价值判断、任务类型、预估规模与修复方向)"] \
            if output["verdict"] in CONFIRMING else []
    reasons = [f"预估改动的文件 {item['path']} 在当前代码中不存在，新建的文件要标明 isNew"
               for item in assessment["estimate"]["files"]
               if not item["isNew"] and not (worktree / item["path"]).is_file()]
    if assessment["worth"] == WorthRecommendation.DEFER.value and not assessment.get("reevaluateWhen"):
        reasons.append("建议暂不修时必须写明重估条件 assessment.reevaluateWhen")
    return reasons


def check(outputs: Mapping[str, Any], worktree: Path, clock: Clock) -> list[ItemResult]:
    context = ScoringContext(project_snapshot=worktree)
    return score_output(Stage.TRIAGE, {"outputs": dict(outputs)}, context, None, clock)


def failures(items: Sequence[ItemResult]) -> list[str]:
    return [f"[{item.item_id}] {item.reason}" for item in items if item.result is ScoreResult.FAIL]
