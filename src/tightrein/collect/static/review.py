"""审查的模型调用：增量审查(collect.static.review)与变体扫描(collect.static.variant)，以及各调用点共用的调用方式。

- 提示一律由 prompts/<调用点>.md 模板拼成，输出按本文件夹的 schema 校验(审查、基线、变体共用 claims.schema.json，
  取证用 verify.schema.json)，调用一律经 agents.call；
- 用到的外部 skill 按名字在调用点加载：加载前用 protocol.vendor 逐文件核对哈希，不符即不调用(抛出，整次巡检失败，
  要人来看是不是被改过)；核对通过的 skill 目录作为可读路径交给 agent，提示里只列 SKILL.md 的路径，按需去读；
- 调用结果归成三类：环境级越界(只读 worktree 被改、碰了禁改路径：整次作废)、预算用尽(额度或用量到限：其余调用不再
  发起)、其余失败(只作废这一次的产出)。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from tightrein.agents.call import AgentContext, call, params_for
from tightrein.agents.params import CallParams
from tightrein.agents.result import CallResult, CallStatus
from tightrein.collect.common.source import SourceUnavailable
from tightrein.collect.static.claims import Claim, ToolFinding
from tightrein.prompts.build import build
from tightrein.protocol import vendor
from tightrein.protocol.boundaries import COMMAND
from tightrein.protocol.handoff import load_schema
from tightrein.protocol.runtime import Runtime

REVIEW = "collect.static.review"
VARIANT = "collect.static.variant"
BASELINE = "collect.static.baseline"
VERIFY = "collect.static.verify"
SCHEMA_FILES = {REVIEW: "claims.schema.json", VARIANT: "claims.schema.json", BASELINE: "claims.schema.json",
                VERIFY: "verify.schema.json"}
SKILLS: dict[str, tuple[str, ...]] = {
    REVIEW: ("differential-review", "sharp-edges"),
    BASELINE: ("sharp-edges",),
    VARIANT: ("variant-analysis",),
    VERIFY: (),
}
EXHAUSTED = frozenset({CallStatus.BUDGET_LIMIT, CallStatus.QUOTA_EXHAUSTED})
TASK_VIOLATIONS = frozenset({COMMAND})  # 只作废那一次调用的越界；其余(只读被改、碰了禁改路径)作废整次巡检
NONE = "(无)"

Caller = Callable[[CallParams, AgentContext], CallResult]


@dataclass(frozen=True)
class Review:
    """一次审查类调用(审查、基线一批、一个变体)的结果。"""

    what: str  # 写进说明：增量审查、基线审查第 2/5 批、缺陷模式 dp-3 的变体扫描
    result: CallResult
    claims: tuple[Claim, ...] = ()
    excluded: tuple[Mapping[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def environment_violated(self) -> bool:
        return violated_environment(self.result)

    @property
    def exhausted(self) -> bool:
        return self.result.status in EXHAUSTED

    def problem(self) -> str | None:
        if self.ok:
            return None
        return f"{self.what} 未完成({self.result.status.value})：{self.result.error or '没有说明'}，其主张不产出信号"


def violated_environment(result: CallResult) -> bool:
    """越界中有环境级的(只读 worktree 被改等)；没有给出种类时按环境级处理。"""
    if result.status is not CallStatus.BOUNDARY:
        return False
    kinds = [item.partition("：")[0] for item in result.violations]
    return not kinds or any(kind not in TASK_VIOLATIONS for kind in kinds)


def call_point(runtime: Runtime, point: str, variables: Mapping[str, str], *, workdir: Path,
               round: int | None = None, caller: Caller = call) -> CallResult:
    """拼提示、加载核对过的 skill、调用模型；schema 不合格等失败都在 CallResult 里。"""
    skills = load_skills(runtime, SKILLS[point])
    schema = schema_for(point)
    model = runtime.settings.model_for(point)
    prompt = build(point, {**variables, "skills": render_skills(skills)}, language=runtime.language, schema=schema,
                   tool=model.tool)
    params = params_for(point, settings=runtime.settings, run=runtime.run, subject=runtime.run, prompt=prompt.text,
                        schema=schema, workdir=workdir, read_paths=tuple(skills.values()), round=round,
                        prompt_hash=prompt.hash)
    return caller(params, runtime.agents)


def review(runtime: Runtime, *, workdir: Path, range_text: str, changes: str, findings: Sequence[ToolFinding],
           knowledge: str, max_claims: int, caller: Caller = call) -> Review:
    variables = {"range": range_text, "changes": changes, "findings": findings_text(findings),
                 "knowledge": knowledge, "max_claims": str(max_claims)}
    return to_review("增量审查", call_point(runtime, REVIEW, variables, workdir=workdir, caller=caller))


def variant(runtime: Runtime, *, workdir: Path, pattern_id: str, pattern: str, tradeoffs: str, number: int,
            caller: Caller = call) -> Review:
    variables = {"pattern_id": pattern_id, "pattern": pattern, "tradeoffs": tradeoffs}
    return to_review(f"缺陷模式 {pattern_id} 的变体扫描",
                     call_point(runtime, VARIANT, variables, workdir=workdir, round=number, caller=caller))


def to_review(what: str, result: CallResult) -> Review:
    if not result.ok or result.output is None:
        return Review(what, result)
    output = result.output
    return Review(what, result, tuple(Claim.from_json(item) for item in output.get("claims", [])),
                  tuple(output.get("excluded", [])))


def findings_text(findings: Sequence[ToolFinding]) -> str:
    if not findings:
        return NONE
    return "```json\n" + json.dumps([item.to_json() for item in findings], ensure_ascii=False, indent=2) + "\n```"


def load_skills(runtime: Runtime, names: Sequence[str]) -> dict[str, Path]:
    """逐个核对哈希后返回 skill 目录；任一不符即抛出，不调用模型。"""
    found = {}
    for name in names:
        try:
            found[name] = vendor.load(runtime.tool, name)
        except vendor.VendorError as error:
            raise SourceUnavailable(f"外部 skill {name} 没有通过核对，不加载：{error}") from error
    return found


def render_skills(skills: Mapping[str, Path]) -> str:
    if not skills:
        return NONE
    return "\n".join(f"- {name}：{path / vendor.SKILL_FILE}" for name, path in skills.items())


@cache
def schema_for(point: str) -> dict[str, Any]:
    return load_schema(Path(__file__).with_name(SCHEMA_FILES[point]))
