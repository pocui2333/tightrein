"""评测计划与变体(architecture/03 2.4、2.7)。

变体 = 版本 × 执行器 × 模型，第一个变体为基线。两种用途：
- version：同一执行器与模型下比较两个版本(改进提案)，候选为 HEAD 加提案的 diff 或当前工作区；
- tool-model：同一版本下比较不同执行器与模型(9.6)，由 --runner 与 --model 的多个取值展开。
变体标签用作输出目录名，只能是单个路径段；每个用例每个变体至少运行 3 次。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Literal

from tightrein.domain.enums import Stage
from tightrein.store.files.layout import segment

MIN_REPEATS = 3
BASELINE = "baseline"
CANDIDATE = "candidate"
HEAD = "HEAD"
DEFAULT_MODEL = "default"

Purpose = Literal["version", "tool-model"]


@dataclass(frozen=True)
class VersionSpec:
    label: str
    commit: str = HEAD
    patch: Path | None = None
    use_worktree: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "commit": self.commit, "patch": None if self.patch is None else str(self.patch),
                "useWorktree": self.use_worktree}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VersionSpec:
        return cls(data["label"], data["commit"], None if data["patch"] is None else Path(data["patch"]),
                   data["useWorktree"])


@dataclass(frozen=True)
class Variant:
    label: str
    version: VersionSpec
    runner: str
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "version": self.version.to_dict(), "runner": self.runner, "model": self.model}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Variant:
        return cls(data["label"], VersionSpec.from_dict(data["version"]), data["runner"], data["model"])


@dataclass(frozen=True)
class EvaluationPlan:
    module: Stage
    case_ids: tuple[str, ...]
    variants: tuple[Variant, ...]
    repeats: int = MIN_REPEATS
    purpose: Purpose = "version"

    def __post_init__(self) -> None:
        if not self.variants:
            raise ValueError("评测计划至少要有一个变体")
        if self.repeats < MIN_REPEATS:
            raise ValueError(f"每个用例每个变体至少运行 {MIN_REPEATS} 次：{self.repeats}")
        labels = [variant.label for variant in self.variants]
        if len(set(labels)) != len(labels):
            raise ValueError(f"变体标签重复：{labels}")
        for variant in self.variants:
            segment(variant.label)
            segment(variant.version.label)
        if self.purpose == "version" and len(self.variants) < 2:
            raise ValueError("版本对比需要基线与至少一个候选")

    @property
    def baseline(self) -> Variant:
        return self.variants[0]

    @property
    def versions(self) -> dict[str, VersionSpec]:
        """按版本标签去重的全部版本，保持首次出现的顺序。"""
        found: dict[str, VersionSpec] = {}
        for variant in self.variants:
            found.setdefault(variant.version.label, variant.version)
        return found

    def to_dict(self) -> dict[str, Any]:
        return {"module": self.module.value, "caseIds": list(self.case_ids),
                "variants": [variant.to_dict() for variant in self.variants], "repeats": self.repeats,
                "purpose": self.purpose}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationPlan:
        return cls(Stage(data["module"]), tuple(data["caseIds"]),
                   tuple(Variant.from_dict(item) for item in data["variants"]), data["repeats"], data["purpose"])


def version_plan(module: Stage, candidate: VersionSpec, runner: str, model: str | None = None,
                 case_ids: Sequence[str] = (), repeats: int = MIN_REPEATS,
                 baseline: VersionSpec = VersionSpec(BASELINE)) -> EvaluationPlan:
    """改进提案的回放对比：基线与候选使用同一执行器与模型。"""
    return EvaluationPlan(module, tuple(case_ids), (
        Variant(baseline.label, baseline, runner, model), Variant(candidate.label, candidate, runner, model),
    ), repeats, "version")


def tool_model_plan(module: Stage, runners: Sequence[str], models: Sequence[str | None] = (None,),
                    version: VersionSpec = VersionSpec(BASELINE), case_ids: Sequence[str] = (),
                    repeats: int = MIN_REPEATS) -> EvaluationPlan:
    """同一版本下 --runner 与 --model 的全部组合；标签为「执行器+模型」。"""
    variants = tuple(Variant(f"{runner}+{model or DEFAULT_MODEL}", version, runner, model)
                     for runner, model in product(runners, models))
    return EvaluationPlan(module, tuple(case_ids), variants, repeats, "tool-model")
