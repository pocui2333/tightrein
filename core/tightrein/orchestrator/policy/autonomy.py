"""自主决定的规则(design 4.12、5.4；配置 autonomy 段与 thresholds.autonomy)。

关卡 gates.issue-approve、gates.plan-confirm 为 auto 时(config/gates.py)，新建的 Issue 与修复计划按下面的规则自动放行或
自动确认，任一条不满足即交用户，理由由调用方写进 Issue 历史、GitHub 镜像评论与事件。这里只有纯函数，不读写存储。
- 放行：每个关联问题都有分诊结论；标签不含「需要先与代码作者讨论」；三类需用户定夺的标记都未命中；影响类别不在
  autonomy.approve.impactKinds；复杂度不高于 autonomy.approve.maxComplexity；严重度不在 autonomy.approve.severities。
- 计划：没有待定问题；三类标记都未命中；没有受保护文件的改动、迁移、新增依赖与删除文件；预估改动不超过
  thresholds.autonomy.planMaxFiles 与 planMaxLines(不大于 thresholds.change)；前端设计说明与计划没有冲突。带拆分的
  计划照常判断，满足时理由中写明拆分的子任务数。计划改动 credentialFiles 或 review.riskRules.authz 命中的文件时属于必须
  交用户的关卡 permissions-secrets。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import Complexity, ImpactKind, IssueLabel
from tightrein.guards.protected import matching_pattern
from tightrein.pipeline.fix.render.plan import FLAG_LABELS

ACTOR = "autonomy"
AUTO_APPROVE = "auto-approve"
NEEDS_DECISION = "needs-decision"
COMPLEXITY_ORDER = (Complexity.LOW, Complexity.MEDIUM, Complexity.HIGH)


@dataclass(frozen=True)
class Decision:
    """approved 为真时 reasons 是满足的条件，为假时是不满足的条件。"""

    approved: bool
    reasons: tuple[str, ...]

    def text(self, approved: str, waiting: str) -> str:
        return f"{approved if self.approved else waiting}：{'；'.join(self.reasons)}"

    def to_dict(self) -> dict[str, Any]:
        return {"approved": self.approved, "reasons": list(self.reasons)}


def _flagged(flags: Mapping[str, Any] | None) -> list[str]:
    return [label for name, label in FLAG_LABELS.items() if ((flags or {}).get(name) or {}).get("flagged")]


def _decision(unmet: Sequence[str], met: Sequence[str]) -> Decision:
    if unmet:
        return Decision(False, tuple(dict.fromkeys(unmet)))
    return Decision(True, tuple(dict.fromkeys(met)))


def approval(config: ProjectConfig, triaged: Sequence[Mapping[str, Any]]) -> Decision:
    """triaged 为 Issue 各关联问题的分诊交接文档 outputs。"""
    impacts = {ImpactKind(item) for item in config.get("autonomy.approve.impactKinds")}
    severities = set(config.get("autonomy.approve.severities"))
    ceiling = Complexity(config.get("autonomy.approve.maxComplexity"))
    unmet: list[str] = [] if triaged else ["没有分诊结论"]
    met: list[str] = ["没有需要先与代码作者讨论或需用户定夺的标记"]
    for outputs in triaged:
        if IssueLabel.DISCUSS_WITH_AUTHOR.value in (outputs.get("labels") or []):
            unmet.append(f"分诊标注「{IssueLabel.DISCUSS_WITH_AUTHOR.label}」")
        unmet += [f"需用户定夺：{label}" for label in _flagged(outputs.get("flags"))]
        kind = ((outputs.get("evidence") or {}).get("impact") or {}).get("kind")
        if kind is not None and ImpactKind(kind) in impacts:
            unmet.append(f"影响类别为{ImpactKind(kind).label}")
        elif kind is not None:
            met.append(f"影响类别为{ImpactKind(kind).label}")
        complexity = Complexity(outputs["complexity"]) if outputs.get("complexity") else None
        if complexity is None or COMPLEXITY_ORDER.index(complexity) > COMPLEXITY_ORDER.index(ceiling):
            unmet.append(f"复杂度为{complexity.label if complexity else '未知'}，高于{ceiling.label}")
        else:
            met.append(f"复杂度为{complexity.label}")
        severity = outputs.get("severity")
        if severity in severities:
            unmet.append(f"严重度 {severity} 需要用户决定")
        elif severity is not None:
            met.append(f"严重度 {severity}")
    return _decision(unmet, met)


def plan_confirmation(config: ProjectConfig, plan: Mapping[str, Any],
                      frontend: Mapping[str, Any] | None) -> Decision:
    """plan 为 plan.json 的内容，frontend 为交接文档的 frontendDesign(没有时为 None)。"""
    max_files = config.whole_threshold("autonomy.planMaxFiles")
    max_lines = config.whole_threshold("autonomy.planMaxLines")
    estimate = plan["estimate"]
    unmet = [f"待定：{item['question']}" for item in plan["userDecisions"]]
    unmet += [f"需用户定夺：{label}" for label in _flagged(plan["flags"])]
    unmet += [f"改动受保护文件 {item['path']}" for item in plan["protectedTouches"]]
    sensitive = (*config.appended("credentialFiles"),
                 *((config.get("review.riskRules") or {}).get("authz") or {}).get("paths", ()))
    unmet += [f"改动权限或密钥配置 {item['path']}(必须交用户：gates.permissions-secrets)" for item in plan.get("files") or ()
              if matching_pattern(item["path"], tuple(sensitive)) is not None]
    if plan["migration"] is not None:
        unmet.append("有数据结构变更(迁移)")
    unmet += [f"新增依赖 {item['name']}" for item in plan["newDependencies"]]
    unmet += [f"删除文件 {item['path']}" for item in plan["deletions"]]
    if estimate["files"] > max_files or estimate["lines"] > max_lines:
        unmet.append(f"预估改动 {estimate['files']} 个文件、{estimate['lines']} 行，超过 {max_files} 个文件、{max_lines} 行")
    design = (frontend or {}).get("design") or {}
    if design.get("planConflicts"):
        unmet.append("前端设计说明与计划有冲突：" + "；".join(design["planConflicts"]))
    met = ["没有待定问题、需用户定夺的标记、受保护文件、迁移、新增依赖与删除文件",
           f"预估改动 {estimate['files']} 个文件、{estimate['lines']} 行"]
    later = (plan.get("split") or {}).get("followUps") or []
    if later:
        met.append(f"拆分为 {len(later) + 1} 个子任务，本计划只做第 1 个")
    return _decision(unmet, met)
