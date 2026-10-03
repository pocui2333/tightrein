"""任务外发现到信号的映射(architecture/04 6.3)。

| 字段 | 取值 |
|---|---|
| source | synthetic |
| check | incidental |
| location | 「文件:类名.方法名」；没有方法名时为文件 |
| message | 发现原文 |
| occurred_at | 来源交接文档的 createdAt；归档条目取报告日期 |
| release | 分诊为 triageCommit，修复为修复分支的基准 commit；归档条目为空 |
| actor | 空 |

context：line、sourcePath、sourceStage、sourceSubject、sourceRunId、sourceDate。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from tightrein.domain.enums import Source
from tightrein.domain.signal import Signal
from tightrein.sources.common.signals import SignalFactory
from tightrein.sources.incidental.locate import Location

CHECK = "incidental"


@dataclass(frozen=True)
class Finding:
    text: str
    location: Location | None
    source_path: str
    stage: str
    occurred_at: datetime
    release: str | None = None
    subject: str | None = None
    run_id: str | None = None


def to_signals(findings: Iterable[Finding], factory: SignalFactory) -> list[Signal]:
    signals = []
    for finding in findings:
        if finding.location is None:
            continue
        signals.append(factory.create(
            source=Source.SYNTHETIC, check=CHECK, location=finding.location.text(), message=finding.text,
            occurred_at=finding.occurred_at, release=finding.release,
            context={
                "line": finding.location.line,
                "sourcePath": finding.source_path,
                "sourceStage": finding.stage,
                "sourceSubject": finding.subject,
                "sourceRunId": finding.run_id,
                "sourceDate": finding.occurred_at.date().isoformat(),
            },
        ))
    return signals
