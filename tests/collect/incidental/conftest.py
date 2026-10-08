from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tightrein.protocol import handoff
from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.naming import FileName
from tightrein.store.files.layout import WorkspaceLayout

TRIAGE_COMMIT = "c" * 40
FIX_BASE = "f" * 40
FINDING = {"file": "src/Services/RefundService.cs", "line": 88, "symbol": "RefundService.Retry", "category": "defect",
           "confidence": "confirmed", "evidence": "Retry() 成功后没有把 ManualReview 置回 false",
           "text": "退款重试后不清除人工复核标记"}


@pytest.fixture
def finding() -> dict[str, Any]:
    return dict(FINDING)


@pytest.fixture
def write_handoff() -> Callable[..., Path]:
    def write(layout: WorkspaceLayout, point: str, subject: str, findings: list[dict[str, Any]], *,
              status: Status = Status.PASSED, round: int | None = None) -> Path:
        facts: dict[str, Any] = {"incidentalFindings": findings}
        if point.startswith("assess"):
            facts["commit"] = TRIAGE_COMMIT
        else:
            facts["baseCommit"] = FIX_BASE
        path = layout.step_file(subject, FileName(point, "handoff", "json", round=round))
        handoff.write(path, Handoff(point=point, subject=subject, run="R-20261004T020000Z-assess", status=status,
                                    summary="结论", facts=facts, created_at="2026-10-04T02:30:00Z"))
        return path

    return write
