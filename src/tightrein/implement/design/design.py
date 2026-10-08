"""方案(implement.design)：拟方案与程序核对。

输入是代码笔记，不再从头探索；重出方案(用户否决、编码发现方案缺口)时直接复用上次通过的定位结论，不回到定位。

程序核对(不合格带原因重出，模型没有返回结果也算一次，共用 controls.implement.design.rounds 的重出上限)：
- 根因假说的每个位置真实存在且不越出 worktree；每处修改位置的文件都在文件清单里；清单中要改的已有非测试文件至少
  有一处修改位置(只新建文件时可以没有)；
- 清单中的已有文件存在；禁改文件不能出现；高风险文件必须写进 protectedTouches；
- 预估改动超出单个 PR 上限而又没说明放不下(oversize)；
- 每条验收标准原文都出现在 acceptanceMapping 中，每一步至少对应一条(对应不上的是未授权的额外改动)；只能在部署后
  确认的验收阶段标准不要求对应(prompts/common.acceptance 不列出它，由发布的验收确认)。

另外两种结局：
- 根因在设计本身(flags.design)而用户还没同意按设计层面修复：停在关卡等用户(Issue 是用户亲自提的需求时视为已同意)；
- 一个 PR 放不下(oversize)：交接带上可拆的几部分，implement.py 据此把 Issue 退回评估拆分。
方案通过后，方案中有前端文件时再出一份前端设计说明(design/frontend.py)，并按方案再判一次风险(design/risk.py)。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.assess.checks import parse_location
from tightrein.implement.check.findings import PLAN_GAP, from_facts
from tightrein.implement.design import frontend, risk
from tightrein.implement.locate.brief import location_problem
from tightrein.implement.prompts import design as prompts
from tightrein.implement.prompts.common import Ask, Usage, acceptance, ask, failure_text, worktree_of
from tightrein.protocol.boundaries import forbidden, high_risk, matching_pattern
from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.recovery import CHECKPOINT_COMMIT
from tightrein.settings.load import Settings

if TYPE_CHECKING:
    from tightrein.implement.context import Decision, ImplementContext
    from tightrein.protocol.runtime import Runtime

POINT = "implement.design"
SCHEMA = Path(__file__).with_name("design.schema.json")
APPROVED = "approve"
REJECTED = "reject"
USER_ORIGIN = "user"
# 交接中不带分析过程(只给人看，放备注)
DROPPED = ("analysis",)
# 方案之后的步骤：其中失败的「方案缺口」与用户的否决是重出方案的原因
LATER = ("implement.approve", "implement.code", "implement.check", "implement.review")


def run(runtime: Runtime, context: ImplementContext) -> Handoff:
    worktree = worktree_of(context)
    previous = context.last(POINT)
    version = _version(previous)
    declined = _declined(context, previous)
    if declined is not None:
        return _handoff(runtime, context, Status.FAILED, f"用户不同意按设计层面修复：{declined.note or '没有说明'}",
                        {"version": version, "reason": "design_declined"}, Usage())
    accepted = design_accepted(context)
    before = risk.plan_risk(context, runtime.settings)
    replan = replan_reasons(context)
    criteria = acceptance(context.body)
    rounds = int(runtime.settings.control(POINT, "rounds"))
    usage = Usage()
    feedback: list[str] = []
    problems: list[str] = []
    for _ in range(rounds + 1):
        variables = prompts.variables(runtime, context, risk=before, replan=replan, feedback=feedback,
                                      design_accepted=accepted)
        result = ask(runtime, context, Ask(POINT, variables, SCHEMA, conditions=risk.conditions(before)), usage)
        if not result.ok or result.output is None:
            problems.append(failure_text(result))
            continue
        plan = dict(result.output)
        if plan["flags"]["design"]["flagged"] and not accepted:
            return _design_issue(runtime, context, plan, version, usage)
        if plan.get("oversize"):
            return _handoff(runtime, context, Status.FAILED, "一个 PR 放不下，退回评估拆成几个 Issue",
                            {"version": version, "reason": "oversize", "splitBack": plan["oversize"],
                             "estimate": plan["estimate"]}, usage, notes=plan.get("analysis"))
        found = check(plan, criteria, worktree, runtime.settings)
        if found:
            feedback = found
            problems += found
            continue
        return _passed(runtime, context, plan, criteria, version, usage)
    return _handoff(runtime, context, Status.FAILED, f"方案重出 {rounds} 次仍未通过程序核对",
                    {"version": version, "reason": "plan_rejected", "problems": problems}, usage)


def check(plan: Mapping[str, Any], criteria: Sequence[str], worktree: Path, settings: Settings) -> list[str]:
    """方案的程序核对；返回全部问题，合格时为空。"""
    files = plan["files"]
    paths = [str(item["path"]) for item in files]
    problems = [f"方案中的已有文件 {item['path']} 不存在" for item in files
                if not item["isNew"] and not _inside_file(worktree, item["path"])]
    problems += [f"新建文件 {item['path']} 越出了代码目录" for item in files
                 if item["isNew"] and not _inside(worktree, item["path"])]
    problems += [f"{path} 是禁改文件，不能出现在方案里" for path in forbidden(paths, settings)]
    touched = {item["path"] for item in plan["protectedTouches"]}
    problems += [f"{path} 是高风险文件，须写进 protectedTouches 并说明改什么、为什么"
                 for path in high_risk(paths, settings) if path not in touched]
    cap = settings.get("boundaries.changeCap")
    estimate = plan["estimate"]
    if estimate["files"] > cap["files"] or estimate["lines"] > cap["lines"]:
        problems.append(f"预估改动 {estimate['files']} 个文件、{estimate['lines']} 行，超出单个 PR 的上限 "
                        f"{cap['files']} 个文件、{cap['lines']} 行：收敛到上限内；确实放不下就填写 oversize")
    problems += _acceptance_problems(plan, criteria)
    problems += hypothesis_problems(plan, worktree, settings)
    return problems


def hypothesis_problems(plan: Mapping[str, Any], worktree: Path, settings: Settings) -> list[str]:
    """根因假说的核对：位置真实存在，修改位置都在文件清单里，要改的已有非测试文件都有修改位置。"""
    hypothesis = plan["hypothesis"]
    problems = [f"根因假说中的位置不存在或越界：{problem}"
                for item in [*hypothesis["evidence"], *hypothesis["edits"]]
                if (problem := location_problem(worktree, item["location"])) is not None]
    planned = {str(item["path"]) for item in plan["files"]}
    edited = {found[0] for item in hypothesis["edits"] if (found := parse_location(item["location"])) is not None}
    problems += [f"修改位置 {item['location']} 的文件不在 files 中" for item in hypothesis["edits"]
                 if (found := parse_location(item["location"])) is not None and found[0] not in planned]
    tests = settings.project.test_patterns if settings.project is not None else ()
    problems += [f"方案要改 {item['path']}，但根因假说没有给出修改位置(hypothesis.edits)" for item in plan["files"]
                 if not item["isNew"] and item["path"] not in edited and matching_pattern(item["path"], tests) is None]
    return problems


def design_accepted(context: ImplementContext) -> bool:
    """用户已同意按设计层面修复(一经记录持续有效)，或 Issue 是用户亲自提的需求(正文就是用户给的修复方向)。"""
    return context.issue.origin == USER_ORIGIN or any(
        item.point == POINT and item.verdict == APPROVED for item in context.decisions)


def replan_reasons(context: ImplementContext) -> list[str]:
    """重出方案的原因：上一版方案之后，用户的否决与编码、自检、审查发现的方案缺口。"""
    previous = context.last(POINT)
    if previous is None:
        return []
    since = previous.created_at or ""
    reasons = []
    for point in LATER:
        found = context.last(point)
        if found is None or found.status is not Status.FAILED or (found.created_at or "") < since:
            continue
        if found.facts.get("rejected"):
            reasons.append(f"用户否决了上一版方案：{found.facts.get('note') or found.summary}")
        reasons += [finding.text() for finding in from_facts(found.facts) if finding.category == PLAN_GAP]
    return reasons


def plan_hash(facts: Mapping[str, Any]) -> str:
    """确认绑定的哈希：方案与前端设计说明；方案一变确认即失效。"""
    bound = {key: value for key, value in facts.items() if key not in ("planHash", "version", CHECKPOINT_COMMIT)}
    return hashlib.sha256(json.dumps(bound, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _passed(runtime: Runtime, context: ImplementContext, plan: dict[str, Any], criteria: Sequence[str], version: int,
            usage: Usage) -> Handoff:
    facts: dict[str, Any] = {key: value for key, value in plan.items() if key not in DROPPED}
    designed = frontend.design(runtime, context, plan, usage)
    facts["frontendFiles"] = designed.files if designed is not None else []
    facts["frontendDesign"] = designed.design if designed is not None else None
    facts["frontendError"] = designed.error if designed is not None else None
    facts["acceptance"] = list(criteria)
    judged = risk.design_risk(plan, context, runtime.settings)
    facts["risk"] = risk.to_facts(judged)
    facts["highRiskPaths"] = list(judged.high_risk_paths)
    facts["planHash"] = plan_hash(facts)
    facts["version"] = version
    summary = f"方案(第 {version} 版)：{plan['summary']}；预估 {plan['estimate']['files']} 个文件、{plan['estimate']['lines']} 行"
    return _handoff(runtime, context, Status.PASSED, summary, facts, usage, notes=plan.get("analysis"))


def _design_issue(runtime: Runtime, context: ImplementContext, plan: Mapping[str, Any], version: int,
                  usage: Usage) -> Handoff:
    reason = plan["flags"]["design"].get("reason") or plan["summary"]
    issue = context.issue.id
    gate = {
        "decision": f"根因在设计本身，局部修补不彻底：{reason}。是否同意按设计层面修复(会扩大修复范围)？",
        "options": [f"同意按设计层面修复：tightrein approve {issue}",
                    f"不按设计层面修，停下交人(找代码作者讨论或不修)：tightrein reject {issue} --note \"<原因>\""],
        "recommendation": "先看方案摘要与理由再定；范围扩大到需要拆分时会退回评估",
        "reason": f"方案摘要：{plan['summary']}",
        "ifNot": "Issue 停在方案，不会自动继续",
        "command": f"tightrein approve {issue}",
    }
    return _handoff(runtime, context, Status.PENDING, f"根因在设计本身，等用户决定是否按设计层面修复：{reason}",
                    {"version": version, "reason": "design_issue", "gate": gate}, usage, notes=plan.get("analysis"))


def _version(previous: Handoff | None) -> int:
    """第几版方案：上一版通过后重出才加一；停在关卡或没通过后重来的仍是同一版。"""
    if previous is None:
        return 1
    found = int(previous.facts.get("version", 1))
    return found + 1 if previous.status is Status.PASSED else found


def _declined(context: ImplementContext, previous: Handoff | None) -> Decision | None:
    """停在「设计问题」关卡之后用户的否决。"""
    if previous is None or previous.status is not Status.PENDING:
        return None
    since = previous.created_at or ""
    return next((item for item in reversed(context.decisions)
                 if item.at > since and item.point == POINT and item.verdict == REJECTED), None)


def _acceptance_problems(plan: Mapping[str, Any], criteria: Sequence[str]) -> list[str]:
    mapping = plan["acceptanceMapping"]
    mapped = {str(item["criterion"]).strip() for item in mapping}
    problems = [f"验收标准「{item}」没有出现在 acceptanceMapping 中" for item in criteria if item.strip() not in mapped]
    covered = {number for item in mapping for number in item["steps"]}
    total = len(plan["steps"])
    problems += [f"acceptanceMapping 引用了不存在的第 {number} 步" for number in sorted(covered) if number > total]
    problems += [f"第 {number} 步「{step['change']}」没有对应任何验收标准，判为未授权的额外改动：去掉或说明它满足哪条"
                 for number, step in enumerate(plan["steps"], start=1) if criteria and number not in covered]
    return problems


def _inside(worktree: Path, path: str) -> bool:
    root = worktree.resolve()
    return root in (root / path).resolve().parents


def _inside_file(worktree: Path, path: str) -> bool:
    return _inside(worktree, path) and (worktree / path).is_file()


def _handoff(runtime: Runtime, context: ImplementContext, status: Status, summary: str, facts: dict[str, Any],
             usage: Usage, *, notes: str | None = None) -> Handoff:
    return Handoff(point=POINT, subject=context.issue.id, run=runtime.run, status=status, summary=summary, facts=facts,
                   notes=notes, metrics=usage.metrics(), versions=usage.versions(runtime))
