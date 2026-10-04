"""勘察到出计划(redesign/05-fix.md 第 2、3 步)与计划的代码检查。

propose 依次：需要时 fix-scout 勘察(位置检查不通过时带原因重做) → 风险判定 → fix-planner 出计划 → 代码检查(不通过时
带原因重出) → 计划含前端文件时 frontend-designer 出前端设计说明(写进计划的 frontendDesign，作为写代码的输入；没有产出时
计划照常给出，原因记在 Proposal.frontend)。预估改动(本计划与拆分出的每个子任务)超出单个 PR 的上限 thresholds.change 时
不通过，要求拆分。根因假说(hypothesis)的证据与修改位置须在 worktree 中真实存在；每处修改位置的文件须在 files 中，
files 中要修改的已有非测试文件须至少有一处修改位置(只新建文件时可以没有)。勘察与出计划共用 thresholds.fix.planRounds 次重做；执行器没有返回 ok 同样计为一次未通过的尝试。
勘察给出 designIssue、计划标记 design 时不再重出，按「设计问题」停下(用户已同意按设计层面修复时照常出计划)。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import RunnerStatus
from tightrein.domain.fix import FixRisk
from tightrein.evaluation.scorers.code import location_problem
from tightrein.guards.protected import matching_pattern
from tightrein.pipeline.fix.prompts import fix_planner, fix_scout, frontend_designer
from tightrein.pipeline.fix.prompts.common import FixCalls
from tightrein.pipeline.fix.prompts.fix_planner import PlanInputs
from tightrein.pipeline.fix.steps import risk as risk_step
from tightrein.pipeline.fix.steps import scout, split
from tightrein.pipeline.fix.steps.context import FixContext

SCOUT = fix_scout.ROLE
PLANNER = fix_planner.ROLE
DESIGNER = frontend_designer.ROLE
FRONTEND_DESIGN = "frontendDesign"


@dataclass(frozen=True)
class PlanCheck:
    problems: tuple[str, ...] = ()
    design: bool = False


def _size_problems(what: str, estimate: Mapping[str, int], max_files: int, max_lines: int) -> list[str]:
    if estimate["files"] <= max_files and estimate["lines"] <= max_lines:
        return []
    return [f"{what}预估改动 {estimate['files']} 个文件、{estimate['lines']} 行，超出上限 {max_files} 个文件、"
            f"{max_lines} 行(不含测试)：拆分为有先后顺序、各自单独成立的子任务，本计划只做第一个，其余写进 split"]


def _location_file(location: str) -> str:
    return location.rsplit(":", 1)[0]


def _hypothesis_problems(plan: Mapping[str, Any], worktree: Path, test_paths: Sequence[str]) -> list[str]:
    hypothesis = plan["hypothesis"]
    locations = [item["location"] for item in [*hypothesis["evidence"], *hypothesis["edits"]]]
    problems = [f"根因假说中的位置不存在或越界：{problem}" for location in locations
                if (problem := location_problem(worktree, location))]
    planned = {item["path"] for item in plan["files"]}
    edited = {_location_file(item["location"]) for item in hypothesis["edits"]}
    problems += [f"修改位置 {item['location']} 的文件不在 files 中" for item in hypothesis["edits"]
                 if _location_file(item["location"]) not in planned]
    problems += [f"计划修改 {item['path']} 但根因假说没有给出修改位置(hypothesis.edits)" for item in plan["files"]
                 if not item["isNew"] and item["path"] not in edited
                 and matching_pattern(item["path"], test_paths) is None]
    return problems


def check(plan: Mapping[str, Any], context: FixContext, worktree: Path, protected_paths: Sequence[str],
          max_files: int, max_lines: int, test_paths: Sequence[str]) -> PlanCheck:
    problems = [f"计划中的已有文件 {item['path']} 不存在" for item in plan["files"]
                if not item["isNew"] and not (worktree / item["path"]).is_file()]
    touched = {item["path"] for item in plan["protectedTouches"]}
    problems += [f"{item['path']} 是受保护文件，须写进 protectedTouches 并说明改什么、为什么" for item in plan["files"]
                 if matching_pattern(item["path"], protected_paths) is not None and item["path"] not in touched]
    problems += _size_problems("本计划", plan["estimate"], max_files, max_lines)
    for number, item in enumerate(split.follow_ups(plan), start=2):
        problems += _size_problems(f"拆分的第 {number} 个子任务「{item['title']}」", item["estimate"], max_files,
                                   max_lines)
    mapped = {item["criterion"].strip() for item in plan["acceptanceMapping"]}
    problems += [f"验收标准「{item}」没有出现在 acceptanceMapping 中" for item in context.acceptance
                 if item not in mapped]
    # 每一步须对应至少一条验收标准(38-external-techniques.md 第 4 项)
    mapped_steps: set[int] = set()
    for item in plan["acceptanceMapping"]:
        for step_num in item.get("steps", []):
            mapped_steps.add(step_num)
    for step_idx, step in enumerate(plan["steps"], start=1):
        if step_idx not in mapped_steps:
            problems.append(f"第 {step_idx} 步「{step.get('change', step.get('file', ''))}」没有对应任何验收标准，"
                            f"判为未授权的额外改动，去掉或补充对应的验收标准")
    # 「不做什么」必填
    if "notDoing" not in plan:
        problems.append("notDoing(不做什么)为必填项，不得省略")
    problems += _hypothesis_problems(plan, worktree, test_paths)
    return PlanCheck(tuple(problems), bool(plan["flags"]["design"]["flagged"]))


@dataclass
class Proposal:
    plan: dict[str, Any] | None = None
    scouting: dict[str, Any] | None = None
    risk: FixRisk | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    design: dict[str, Any] | None = None
    frontend: dict[str, Any] | None = None

    def record(self, role: str, status: RunnerStatus) -> None:
        self.attempts.append({"role": role, "status": status.value})


@dataclass(frozen=True)
class ProposalSettings:
    worktree: Path
    config: ProjectConfig
    protected: tuple[str, ...]
    max_files: int
    max_lines: int
    rounds: int
    endpoints: Collection[str] = ()
    review_notes: tuple[str, ...] = ()
    scout: bool = False  # B 通道需要勘察时(lanes.needs_scout)
    large: bool = False  # C 通道：整体方案并拆分
    # 用户已同意按设计层面的根因修复(fix plan --accept-design)：设计问题不再中止，照常出计划
    design_accepted: bool = False
    test_paths: tuple[str, ...] = ()  # 测试文件不要求根因假说给出修改位置


def _design_from_flags(plan: Mapping[str, Any]) -> dict[str, Any]:
    flag = plan["flags"]["design"]
    return {"rootCause": plan["summary"], "reason": flag.get("reason") or "", "locations": flag.get("locations") or []}


def _scout(calls: FixCalls, context: FixContext, settings: ProposalSettings, proposal: Proposal) -> bool:
    """勘察；位置检查不通过时带原因重做。返回是否得到合格的勘察结论(设计问题也算得到)。"""
    feedback: list[str] = []
    for attempt in range(1, settings.rounds + 2):
        result = calls.run(fix_scout.task(calls.prompt, context, attempt, feedback))
        proposal.record(SCOUT, result.status)
        if result.status is not RunnerStatus.OK or result.output is None:
            proposal.problems.append(f"fix-scout：{scout.status_text(result.status, result.error_type)}")
            continue
        found = scout.check(result.output, settings.worktree)
        if found:
            feedback = scout.feedback(found)
            proposal.problems += feedback
            continue
        proposal.scouting = dict(result.output)
        if result.output.get("designIssue") and not settings.design_accepted:
            proposal.design = proposal.scouting["designIssue"]
        return True
    return False


def propose(calls: FixCalls, context: FixContext, settings: ProposalSettings) -> Proposal:
    proposal = Proposal()
    if settings.scout and (not _scout(calls, context, settings, proposal) or proposal.design is not None):
        return proposal
    proposal.risk = risk_step.plan_risk(context, proposal.scouting, settings.config, settings.endpoints)
    feedback: list[str] = []
    for attempt in range(1, settings.rounds + 2):
        inputs = PlanInputs(proposal.scouting, proposal.risk, settings.protected, settings.max_files,
                            settings.max_lines, settings.review_notes, settings.large)
        result = calls.run(fix_planner.task(calls.prompt, context, inputs, attempt, feedback))
        proposal.record(PLANNER, result.status)
        if result.status is not RunnerStatus.OK or result.output is None:
            proposal.problems.append(f"fix-planner：{scout.status_text(result.status, result.error_type)}")
            continue
        verdict = check(result.output, context, settings.worktree, settings.protected, settings.max_files,
                        settings.max_lines, settings.test_paths)
        if verdict.design and not settings.design_accepted:
            proposal.plan = dict(result.output)
            proposal.design = _design_from_flags(result.output)
            return proposal
        if verdict.problems:
            feedback = list(verdict.problems)
            proposal.problems += feedback
            continue
        proposal.plan = {**result.output, "risk": proposal.risk.to_dict()}
        proposal.plan.pop(FRONTEND_DESIGN, None)
        _design_frontend(calls, context, settings.config, proposal, attempt)
        return proposal
    return proposal


def _design_frontend(calls: FixCalls, context: FixContext, config: ProjectConfig, proposal: Proposal,
                     attempt: int) -> None:
    """计划含前端文件时运行一次 frontend-designer；proposal.frontend 为交接文档的 frontendDesign。"""
    files = frontend_designer.frontend_files(config, proposal.plan)
    if not files:
        return
    result = calls.run(frontend_designer.task(calls.prompt, context, proposal.plan, files, attempt))
    proposal.record(DESIGNER, result.status)
    if result.status is RunnerStatus.OK and result.output is not None:
        proposal.plan[FRONTEND_DESIGN] = dict(result.output)
        proposal.frontend = {"files": files, "design": dict(result.output), "error": None}
        return
    proposal.frontend = {"files": files, "design": None,
                         "error": f"{DESIGNER}：{scout.status_text(result.status, result.error_type)}"}
