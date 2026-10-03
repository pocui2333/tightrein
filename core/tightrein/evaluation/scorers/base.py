"""评分的公共结构(architecture/03 2.4)：逐项结果、评分上下文与代码评分器的接口。

评分表与评分器在生产运行与评测中是同一份：生产中由调用模块构造 ScoringContext(没有用例)，评测中由评测运行器
构造(带用例、代码快照与沙箱输出目录)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from tightrein.domain.enums import ScoreMethod, ScoreResult, Stage
from tightrein.evaluation.cases import ModuleCase
from tightrein.guards.policy import GuardSettings
from tightrein.runner.task import Subject


@dataclass(frozen=True)
class ItemResult:
    item_id: str
    method: ScoreMethod
    result: ScoreResult
    reason: str
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"itemId": self.item_id, "method": self.method.value, "result": self.result.value,
                "reason": self.reason, "evidence": list(self.evidence)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ItemResult:
        return cls(data["itemId"], ScoreMethod(data["method"]), ScoreResult(data["result"]), data["reason"],
                   tuple(data["evidence"]))


@dataclass(frozen=True)
class ScoreOutcome:
    """一个代码评分项的结论。"""

    result: ScoreResult
    reason: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgeRequest:
    """模型评审挂在哪次运行、哪个环节与对象下，用哪个工具与模型；费用计入 stage 的预算。"""

    run_id: str
    stage: Stage
    subject: Subject
    tool: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class ScoringContext:
    case: ModuleCase | None = None
    project_snapshot: Path | None = None
    output_dir: Path | None = None
    diff_text: str | None = None
    guard_settings: GuardSettings = field(default_factory=GuardSettings)
    judge: JudgeRequest | None = None


class CodeScorer(Protocol):
    def __call__(self, outputs: Mapping[str, Any], params: Mapping[str, Any],
                 context: ScoringContext) -> ScoreOutcome: ...
