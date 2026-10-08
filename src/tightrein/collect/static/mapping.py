"""成立的主张 → 信号；规则库的命中 → 信号。

- 只有判定为成立(confirmed)或有条件成立(conditional)的主张产出信号；不成立与证据不足只计数；
- location 为「文件:行」：优先取取证给出的第一个根因(symbol 为它的「类名.方法名」)，没有时用主张的位置；依赖漏洞写
  「依赖清单:包名」；
- 用 git blame 标出引入的提交(commit、作者、日期)，blame 失败不影响信号；
- verified、deterministic 都为真：已逐条取证，去重不走「偶发先观察」；评估复用这里的取证结论(evidence.verification)；
- 规则库的命中(规则已用原补丁验证过)不经审查与取证，直接产出信号，check 为 `rule:<规则编号>`。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from tightrein.collect.common.signals import Signal, SignalFactory
from tightrein.collect.static.claims import CHECK_TYPE, ToolFinding
from tightrein.collect.static.verify import CONDITIONAL, SIGNAL_VERDICTS, Verification
from tightrein.protocol.git import Git, GitError
from tightrein.protocol.naming import format_iso

RULE_PREFIX = "rule:"


def introduced_by(git: Git, file: str, line: int, head: str) -> dict[str, Any] | None:
    try:
        lines = git.blame(file, line, line, head)
    except GitError:
        return None
    if not lines:
        return None
    blamed = lines[0]
    return {"commit": blamed.commit, "author": blamed.author, "date": format_iso(blamed.time)}


def location(item: Verification, finding: ToolFinding | None) -> tuple[str, str | None, int]:
    """(location, symbol, 用于 blame 的行)。"""
    if finding is not None and finding.vulnerability and finding.package:
        return f"{finding.file}:{finding.package['name']}", None, item.claim.line
    causes = (item.output or {}).get("rootCauses") or []
    if causes:
        cause = causes[0]
        line = int(cause.get("line") or item.claim.line)
        return f"{cause['file']}:{line}", cause.get("symbol") or None, line
    return f"{item.claim.file}:{item.claim.line}", None, item.claim.line


def to_signals(items: Iterable[Verification], findings: Sequence[ToolFinding], factory: SignalFactory, *,
               head: str, git: Git) -> list[Signal]:
    by_rule = {(finding.file, finding.rule): finding for finding in findings}
    signals = []
    for item in items:
        output = item.output
        if output is None or item.verdict not in SIGNAL_VERDICTS:
            continue
        finding = by_rule.get((item.claim.file, item.claim.rule_or_pattern))
        where, symbol, line = location(item, finding)
        impact: Mapping[str, Any] = output.get("impact") or {}
        signals.append(factory.create(
            check_type=CHECK_TYPE, location=where, symbol=symbol, message=item.claim.statement,
            occurred_at=item.completed_at, commit=head, deterministic=True, verified=True,
            evidence={
                "rule": item.claim.rule_or_pattern,
                "line": item.claim.line,
                "layer": item.claim.layer,
                "severity": item.claim.severity,
                "verdict": {"value": item.verdict,
                            "condition": output.get("trigger") if item.verdict == CONDITIONAL else None},
                "facts": [dict(fact) for fact in output.get("facts") or []],
                "callChain": list(impact.get("callSites") or []),
                "introducedBy": introduced_by(git, where.partition(":")[0], line, head),
                "toolFinding": None if finding is None else finding.to_json(),
                "verification": dict(output),
            },
        ))
    return signals


def rule_signals(findings: Iterable[ToolFinding], factory: SignalFactory, *, head: str, now: datetime) -> list[Signal]:
    return [factory.create(check_type=CHECK_TYPE, location=f"{item.file}:{item.line or 1}", message=item.message,
                           occurred_at=now, commit=head, deterministic=True,
                           evidence={"rule": f"{RULE_PREFIX}{item.rule}", "line": item.line,
                                     "toolFinding": item.to_json()})
            for item in findings]
