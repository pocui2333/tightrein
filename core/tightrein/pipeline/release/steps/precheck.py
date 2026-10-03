"""提交前置条件(architecture/07 19.1 第 1 步，design 7.2)：Issue 进行中且处于提交阶段；修复交接文档为 ok 且最后一轮的确定性检查
(含交付规则)全部通过。不满足时列出未通过的检查项、位置与问题，由用户决定先处理(fix apply --review-only 或 fix start)，
还是以 --accept-findings 照常提交(接受的未通过项写进 Issue 历史与 PR 描述)。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tightrein.domain.enums import HandoffStatus, IssuePhase
from tightrein.domain.issue import Issue, in_phase


@dataclass(frozen=True)
class Precheck:
    ok: bool
    reason: str | None = None
    failures: tuple[dict[str, Any], ...] = ()
    accepted: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def check(issue: Issue, fix: tuple[str, Mapping[str, Any]] | None, accept_findings: bool) -> Precheck:
    if not in_phase(issue, IssuePhase.SUBMIT):
        return Precheck(False, f"Issue 当前为「{issue.status.label}」，合并前验证通过后才能提交")
    if fix is None or fix[0] != HandoffStatus.OK.value:
        return Precheck(False, "修复没有通过评审，先执行 fix apply")
    rounds = fix[1].get("rounds") or []
    last = rounds[-1] if rounds else None
    failures = tuple({"check": item["check"], "location": item["location"], "problem": item["problem"]}
                     for item in (last or {}).get("failures", []))
    if last is not None and last["checksPassed"] and not failures:
        return Precheck(True)
    if accept_findings:
        return Precheck(True, failures=failures, accepted=failures)
    listing = "；".join(f"{item['check']} {item['location'] or ''} {item['problem']}".replace("  ", " ")
                       for item in failures) or "最后一轮没有确定性检查的结果"
    return Precheck(False, f"最后一轮确定性检查没有全部通过：{listing}；先执行 fix apply --review-only 或 fix start，"
                    "或以 --accept-findings 照常提交", failures)
