"""任务外发现 → 信号：按类别过滤，按「文件 + 符号 + 类别」作为指纹。

| 字段 | 取值 |
|---|---|
| check_type | `incidental:<类别>` |
| location | 有行号时为「文件:行号」，否则为文件 |
| symbol | 发现给的符号 |
| message | 发现原文 |
| occurred_at | 交接文档的生成时间 |
| commit | 产生它的环节的基准 commit |
| group_key | `incidental:<文件>:<符号>:<类别>`：不用行号，行号变化不再拆成两个；同一处的不同类别不再并成一个 |

类别只认缺陷、安全、性能、数据，其他(命名、风格、重构建议)丢弃并计数。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from tightrein.collect.common.signals import Signal, SignalFactory

CATEGORIES = {"defect": "缺陷", "security": "安全", "performance": "性能", "data": "数据"}
CHECK_PREFIX = "incidental:"


@dataclass(frozen=True)
class Finding:
    file: str
    line: int | None
    symbol: str | None
    category: str
    confidence: str
    evidence: str
    text: str
    source_path: str  # 交接文档，相对工作区
    point: str  # 产生它的步骤
    subject: str
    run: str
    occurred_at: datetime
    commit: str | None


def to_signals(findings: Iterable[Finding], factory: SignalFactory) -> tuple[list[Signal], int]:
    """返回 (信号, 因类别不收而丢弃的条数)。"""
    signals = []
    dropped = 0
    for finding in findings:
        if finding.category not in CATEGORIES:
            dropped += 1
            continue
        signals.append(factory.create(
            check_type=f"{CHECK_PREFIX}{finding.category}",
            location=f"{finding.file}:{finding.line}" if finding.line else finding.file, symbol=finding.symbol,
            message=finding.text, occurred_at=finding.occurred_at, commit=finding.commit,
            group_key=fingerprint(finding),
            evidence={"confidence": finding.confidence, "evidence": finding.evidence, "line": finding.line,
                      "sourcePath": finding.source_path, "sourcePoint": finding.point,
                      "sourceSubject": finding.subject, "sourceRun": finding.run},
        ))
    return signals, dropped


def fingerprint(finding: Finding) -> str:
    return f"{CHECK_PREFIX}{finding.file}:{finding.symbol or ''}:{finding.category}"
