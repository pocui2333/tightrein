"""取证成立的主张到信号的映射(architecture/04 5.5)。

只有判定为 confirmed 或 conditional 的主张产出信号：source 为 synthetic，check 为主张的 ruleOrPattern，
location 为取证给出的「文件:类名.方法名」(依赖漏洞为「依赖清单文件:包名」，取证没有给出位置时为主张的文件)，
message 为主张的一句话描述，occurred_at 为取证完成时间，release 为本次巡检的 HEAD，actor 为空。
context：line、layer、verdict(判定与条件)、evidence(证据位置与说明)、callChain(调用点)、introducedBy
(git blame 得到的 commit、作者与日期)、toolFinding(来自确定性工具时的原始条目)、transcripts(审查与取证的会话记录)、
verification(完整的取证输出，分诊直接对它做证据检查)。
规则库的命中(rule_signals)不经取证：check 为 rule:<规则编号>，location 为「文件:行」，context 为行、规则编号与消息，
由分诊照常处理。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import Source, Verdict
from tightrein.domain.signal import Signal
from tightrein.sources.common.signals import SignalFactory
from tightrein.sources.static.reviewer import ToolFinding, VerifiedClaim
from tightrein.vcs.errors import VcsError
from tightrein.vcs.git_read import GitReader

SIGNAL_VERDICTS = (Verdict.CONFIRMED, Verdict.CONDITIONAL)


def introduced_by(git: GitReader, repo: Path, file: str, line: int, head: str) -> dict[str, Any] | None:
    try:
        lines = git.blame(repo, file, line, line, head)
    except VcsError:
        return None
    if not lines:
        return None
    blamed = lines[0]
    return {"commit": blamed.commit, "author": blamed.author, "date": format_iso(blamed.time)}


def location(item: VerifiedClaim) -> str:
    finding = item.tool_finding
    if finding is not None and finding.vulnerability and finding.package:
        return f"{finding.file}:{finding.package['name']}"
    return item.verification.location() or item.claim.file


def to_signals(items: Iterable[VerifiedClaim], factory: SignalFactory, head: str, now: datetime) -> list[Signal]:
    signals = []
    for item in items:
        verification = item.verification
        verdict = verification.verdict
        if verdict not in SIGNAL_VERDICTS or verification.output is None:
            continue
        output = verification.output
        impact = output.get("impact") or {}
        signals.append(factory.create(
            source=Source.SYNTHETIC, check=item.claim.rule_or_pattern, location=location(item),
            message=item.claim.statement, occurred_at=verification.completed_at or now, release=head,
            context={
                "line": item.claim.line,
                "layer": item.claim.layer,
                "verdict": {"value": verdict.value,
                            "condition": output.get("trigger") if verdict is Verdict.CONDITIONAL else None},
                "evidence": [dict(fact) for fact in output.get("facts") or []],
                "callChain": list(impact.get("callSites") or []),
                "introducedBy": item.introduced_by,
                "toolFinding": None if item.tool_finding is None else item.tool_finding.to_dict(),
                "transcripts": [path for path in (item.review_transcript, verification.transcript) if path],
                "verification": dict(output),
            },
        ))
    return signals


RULE_CHECK = "rule:"


def rule_signals(findings: Iterable[ToolFinding], factory: SignalFactory, head: str, now: datetime) -> list[Signal]:
    return [factory.create(source=Source.SYNTHETIC, check=f"{RULE_CHECK}{item.rule}",
                           location=f"{item.file}:{item.line}", message=item.message, occurred_at=now, release=head,
                           context={"line": item.line, "rule": {"id": item.rule, "message": item.message},
                                    "toolFinding": item.to_dict()})
            for item in findings]
