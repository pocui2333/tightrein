"""编码(implement.code)：照已确认的方案改代码，逻辑、接口、数据处理类改动同时写测试。

- 唯一可写的步骤：只写自己的 worktree；允许的命令是项目检查命令(测试命令去掉 `{tests}` 后的前缀也算)加只读命令；
- 编码前核对定案确认的就是当前方案(方案哈希)，不一致说明流程错了，直接报错；
- 下一轮续接上一轮的会话(格式不符的续接由 agents.call 按 limits.decide 处理)；第二轮起先按上一轮的自检结果记
  检查点或退回检查点(code/checkpoint.py)，退回说明放在修正说明最前面；
- 一轮结束后程序检查本轮改动(protocol.boundaries.check_round)：越出 worktree、碰了禁改文件即撤回本轮全部改动，
  交接带上越界原因；implement.py 第一次越界时带原因重做这一轮，再越界停下。改动量超上限由自检按「先收敛一次」处理；
- 结局分类：调用失败为局部问题(下一轮重来)；模型中止(aborted)为方案缺口(带说明重出方案)；
- 发布合并 main 冲突退回编码时(code/merge.py)：这一轮先由程序合并那个 main，冲突说明放在修正说明最前面，本轮改动
  相对合并进来的 main 算；冲突都解决了由程序完成合并提交；没解决完或这一轮没通过(调用失败、中止、越界)就放弃合并、
  worktree 连同分支回到合并前，下一轮重来；这一轮中止说明两侧是互斥实现，归为需要用户(选保留哪一侧)，不是方案缺口。
  没有冲突标记、改文件解决不了的冲突(一侧删了另一侧改了、二进制文件等)不调用模型，放弃合并后归为需要用户，
  关卡请用户选保留哪一侧；选了之后的轮次由程序先把这些文件写成那一侧；
- 撤回(越界、放弃合并)用检查点恢复(prepare/workspace.restore)，不用 `merge --abort`：合并后自动合并并已暂存的文件
  被 agent 改过时它会失败；恢复本身失败也不抛出，交接带上需要用户的不通过项。撤回过的交接把检查点(worktreeCommit)
  记成这一轮开始时，不让 implement 现取快照：恢复失败时 worktree 停在半截状态，记成检查点后每次续跑都会退回它。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.agents.params import Access
from tightrein.agents.result import CallResult, CallStatus
from tightrein.implement.approve.approve import confirmed
from tightrein.implement.check.findings import LOCAL, NEEDS_USER, PLAN_GAP, Finding, from_facts, of_first_category
from tightrein.implement.code import merge
from tightrein.implement.code.checkpoint import Checkpoint
from tightrein.implement.design import risk
from tightrein.implement.prepare import workspace
from tightrein.implement.prompts import code as prompts
from tightrein.implement.prompts.common import Ask, Usage, ask, design_documents, failure_text, worktree_of
from tightrein.protocol.boundaries import FORBIDDEN, OUTSIDE, Change, Violation, check_round, counted
from tightrein.protocol.git import GitError
from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.recovery import CHECKPOINT_COMMIT

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext
    from tightrein.protocol.git import Git
    from tightrein.protocol.runtime import Runtime

POINT = "implement.code"
DESIGN = "implement.design"
CHECK = "implement.check"
REVIEW = "implement.review"
SCHEMA = Path(__file__).with_name("code.schema.json")
CHECK_NAME = "code"
ABORTED = "aborted"
# 编码这一步自己处理的越界(撤回重做)；改动量超上限交给自检按「先收敛一次」处理
REVERTED_KINDS = frozenset({OUTSIDE, FORBIDDEN})
CHECKPOINT_DIR = "checkpoint"
TEST_COMMAND = "test"
PASSED = "passed"


def run(runtime: Runtime, context: ImplementContext) -> Handoff:
    worktree = worktree_of(context)
    git, base = _git(context), _base(context)
    design = context.last(DESIGN)
    if design is None or not confirmed(context):
        raise ValueError(f"Issue {context.issue.id} 的方案还没有确认，或确认的不是当前方案")
    previous = context.last(POINT)
    redo = _redo(context, previous)
    sync = merge.pending(context, git)
    corrections = _violations_note(previous) if redo else corrections_for(context)
    rolled_back: int | None = None
    if not redo and sync is None:
        note, rolled_back = _checkpoint(runtime, context, git, worktree, base)
        if note:
            corrections.insert(0, note)
    start = workspace.snapshot(git)  # 合并 main 之前：这一轮没通过时连同合并一起撤回
    conditions = risk.conditions(context.risk)
    facts: dict[str, Any] = {"baseCommit": base, "sessionId": None, "conditions": list(conditions), "redo": redo,
                             "corrections": corrections, "rolledBackTo": rolled_back}
    merging = merge.Merging()
    if sync is not None:
        base = facts["baseCommit"] = sync.main  # 合并完成后 review_base 就是它：本轮改动相对它算
        try:
            merging = merge.start(runtime, context.issue.id, git, sync)
            side = merge.chosen(context, sync) if merging.unmergeable else None
            if side is not None:
                merging = merge.choose(git, merging, side)
        except GitError as error:
            blocker = Finding(CHECK_NAME, "merge_failed", None, f"程序合并 main 失败，已放弃这次合并：{error}", NEEDS_USER)
            return _failed(runtime, context, git, start, blocker, facts, Usage())
        facts["mergedMain"] = merging.facts(sync)
        if merging.unmergeable and merging.side is None:
            return _unmergeable(runtime, context, git, start, sync, merging, previous, facts)
        corrections.insert(0, merge.note(git, sync, merging))
    usage = Usage()
    result = ask(runtime, context, _request(runtime, context, design.facts, corrections, previous, conditions), usage)
    facts["sessionId"] = result.session_id
    changes = git.numstat(base)
    violations = _violations(runtime, changes, worktree, result)
    if violations:
        blockers = [Finding(CHECK_NAME, item.kind, item.path, item.detail, NEEDS_USER) for item in violations]
        return _handoff(runtime, context, Status.FAILED, "本轮改动越界，已撤回：" + "；".join(v.detail for v in violations),
                        {**facts, "violations": [asdict(item) for item in violations], CHECKPOINT_COMMIT: start,
                         "blockers": [item.to_json() for item in blockers + _restore(git, start)]}, usage)
    if not result.ok or result.output is None:
        blocker = Finding(CHECK_NAME, "executor", None, failure_text(result), LOCAL)
        return _failed(runtime, context, git, start if sync is not None else None, blocker, facts, usage)
    output = result.output
    if output["status"] == ABORTED:
        big = output["bigIssue"]
        # 合并 main 的这一轮中止：两侧是互斥实现，要用户选保留哪一侧，不是方案缺口
        blocker = Finding(CHECK_NAME, "merge_conflict", "、".join(big["locations"]) or None,
                          f"合并 main 的冲突无法兼顾两侧：{big['description']}", NEEDS_USER) if merging.conflicted else \
            Finding(CHECK_NAME, "big_issue", "、".join(big["locations"]) or None,
                    f"实施中止，方案走不通：{big['description']}", PLAN_GAP)
        return _failed(runtime, context, git, start if sync is not None else None, blocker,
                       {**facts, **_reported(output)}, usage, notes=output["analysis"])
    left = merge.unresolved(git, merging)
    if left:
        blocker = Finding(CHECK_NAME, "merge_conflict", "、".join(left),
                          f"合并 main 的冲突没解决完，已放弃这次合并，下一轮重新合并：{'、'.join(left)}", LOCAL)
        return _failed(runtime, context, git, start, blocker, {**facts, **_reported(output)}, usage,
                       notes=output["analysis"])
    if sync is not None:
        try:
            facts["mergedMain"]["commit"] = merge.finish(runtime, context.issue.id, git, merging) \
                if merging.conflicted else git.head().commit
        except GitError as error:
            blocker = Finding(CHECK_NAME, "merge_commit", None,
                              f"完成合并提交失败，已放弃这次合并，下一轮重新合并：{error}", LOCAL)
            return _failed(runtime, context, git, start, blocker, {**facts, **_reported(output)}, usage,
                           notes=output["analysis"])
    files, lines = counted(changes, runtime.settings)
    facts.update(_reported(output))
    facts["changedFiles"] = [{"path": item.path, "added": item.added, "deleted": item.deleted} for item in changes]
    facts["linesAdded"] = sum(item.added for item in changes)
    facts["linesDeleted"] = sum(item.deleted for item in changes)
    facts["diffHash"] = git.diff_hash(base)
    facts["blockers"] = []
    summary = f"第 {context.round} 轮编码完成：改动 {len(changes)} 个文件(计入上限的 {files} 个、{lines} 行)"
    return _handoff(runtime, context, Status.PASSED, summary, facts, usage, notes=output["analysis"],
                    files_changed=files, lines_changed=lines)


def corrections_for(context: ImplementContext) -> list[str]:
    """上一轮没通过的那一步(审查、自检或编码本身)中最靠前一类的局部问题：只把这一类交回编码。"""
    found = failing(context, context.round - 1)
    if found is None:
        return []
    chosen = of_first_category(from_facts(found.facts))
    return [item.text() for item in chosen if item.category == LOCAL]


def failing(context: ImplementContext, number: int) -> Handoff | None:
    """第 number 轮最后一个没通过的编码、自检或审查交接。"""
    found = [handoff for point in (POINT, CHECK, REVIEW)
             if (handoff := context.last(point)) is not None and handoff.round == number
             and handoff.status is Status.FAILED]
    return max(found, key=lambda item: item.created_at or "", default=None)


def _request(runtime: Runtime, context: ImplementContext, design: dict[str, Any], corrections: list[str],
             previous: Handoff | None, conditions: tuple[str, ...]) -> Ask:
    commands = prompts.allowed_commands(runtime)
    session = previous.facts.get("sessionId") if previous is not None else None
    # 续接同一会话：条件相同(工具与模型不变)且不是新一版方案的第一轮
    if session and previous is not None and previous.facts.get("conditions") == list(conditions) \
            and _same_plan(context, previous):
        return Ask(prompts.CONTINUE, prompts.continue_variables(runtime, context, corrections), SCHEMA,
                   conditions=conditions, access=Access.WRITE, allowed_commands=commands, resume_session=str(session),
                   round=context.round, read_paths=design_documents(runtime))
    return Ask(POINT, prompts.variables(runtime, context, design, corrections), SCHEMA, conditions=conditions,
               access=Access.WRITE, allowed_commands=commands, round=context.round,
               read_paths=design_documents(runtime))


def _checkpoint(runtime: Runtime, context: ImplementContext, git: Git, worktree: Path,
                base: str) -> tuple[str | None, int | None]:
    """按上一轮的自检结果记检查点或退回；新一版方案的第一轮清掉旧检查点。"""
    point = Checkpoint(git, worktree, base, runtime.workspace.subject_dir(context.issue.id) / CHECKPOINT_DIR)
    checked = context.last(CHECK)
    if checked is None or not _same_plan(context, checked):
        point.clear()
        return None, None
    if checked.round != context.round - 1:  # 上一轮编码没走到自检：沿用已有检查点，不比较
        return None, None
    tests_passed = tests_passed_in(checked.facts)
    findings = len(from_facts(checked.facts))
    threshold = int(runtime.settings.section(POINT)["checkpointRollbackFindings"])
    changed = git.changed_files(base)
    if point.should_rollback(tests_passed, findings, threshold):
        reason = "测试重新失败" if not tests_passed else f"有 {findings} 项不通过"
        restored = point.round
        return point.rollback(changed, f"第 {checked.round} 轮{reason}"), restored
    if point.should_save(tests_passed, findings):
        point.save(context.round - 1, changed, findings)
    return None, None


def tests_passed_in(facts: dict[str, Any]) -> bool:
    """自检交接中的测试命令都跑了且通过(编码写的测试与受影响的测试都在其中)。"""
    tests = [item for item in facts.get("commands") or [] if item.get("name") == TEST_COMMAND]
    return bool(tests) and all(item.get("result") == PASSED for item in tests)


def _violations(runtime: Runtime, changes: Sequence[Change], worktree: Path, result: CallResult) -> list[Violation]:
    """本轮的越界：程序检查改动(越出 worktree、禁改文件)，加上调用时边界比对发现的。"""
    found = [item for item in check_round(changes, worktree=worktree, settings=runtime.settings,
                                          project=runtime.settings.project) if item.kind in REVERTED_KINDS]
    if result.status is CallStatus.BOUNDARY:
        found += [Violation(OUTSIDE, None, text) for text in result.violations or [result.error or "调用越界"]]
    return found


def _failed(runtime: Runtime, context: ImplementContext, git: Git, start: str | None, blocker: Finding,
            facts: dict[str, Any], usage: Usage, *, notes: str | None = None) -> Handoff:
    """这一轮没通过：start 不为空时先撤回到这一轮开始时(合并 main 的这一轮连同合并一起放弃，下一轮重新合并)，
    检查点记成 start；为空时改动留着，检查点由 implement 现取。"""
    blockers = [blocker, *(_restore(git, start) if start is not None else [])]
    checkpoint = {CHECKPOINT_COMMIT: start} if start is not None else {}
    return _handoff(runtime, context, Status.FAILED, blocker.summary,
                    {**facts, **checkpoint, "blockers": [item.to_json() for item in blockers]}, usage, notes=notes)


def _unmergeable(runtime: Runtime, context: ImplementContext, git: Git, start: str, sync: merge.Sync,
                 merging: merge.Merging, previous: Handoff | None, facts: dict[str, Any]) -> Handoff:
    """合并 main 有改文件解决不了的冲突、用户还没选保留哪一侧：放弃这次合并，不调用模型，停在关卡等用户选。
    会话接着用上一轮的，选了之后那一轮照常续接。"""
    listed = "；".join(f"{item.path}({item.kind})" for item in merging.unmergeable)
    blockers = [Finding(CHECK_NAME, "merge_conflict", item.path,
                        f"合并 main 时{item.kind}，改文件解决不了，要你选保留哪一侧", NEEDS_USER)
                for item in merging.unmergeable]
    blockers += _restore(git, start)
    session = previous.facts.get("sessionId") if previous is not None else None
    return _handoff(runtime, context, Status.FAILED, f"合并 main 有改文件解决不了的冲突，已放弃这次合并，要你选保留哪一侧：{listed}",
                    {**facts, "sessionId": session, "blockers": [item.to_json() for item in blockers],
                     CHECKPOINT_COMMIT: start, "gate": merge.gate(context.issue.id, sync, merging)}, Usage())


def _restore(git: Git, start: str) -> list[Finding]:
    """撤回到这一轮开始时(连同进行中或已提交的合并、agent 自己的提交)；恢复本身失败时不抛出，返回需要用户的不通过项。
    调用方把交接的检查点记成 start(见模块说明)：用户处理好之后续跑时 implement 照它再恢复一次。"""
    try:
        workspace.restore(git, start)
    except GitError as error:
        return [Finding(CHECK_NAME, "restore_failed", None,
                        f"撤回本轮改动失败，worktree 可能停在半截状态，要人看一下：{error}", NEEDS_USER)]
    return []


def _redo(context: ImplementContext, previous: Handoff | None) -> bool:
    """这一轮是越界撤回后的重做：上一次编码就是这一轮，且因越界没通过。"""
    return (previous is not None and previous.round == context.round and previous.status is Status.FAILED
            and bool(previous.facts.get("violations")))


def _violations_note(previous: Handoff | None) -> list[str]:
    details = [str(item.get("detail")) + (f"：{item['path']}" if item.get("path") else "")
               for item in (previous.facts.get("violations") if previous is not None else None) or []]
    return ["上一次的改动越界，已全部撤回，按方案重做这一轮；不要再改 worktree 之外与禁改的文件：" + "；".join(details)]


def _same_plan(context: ImplementContext, handoff: Handoff) -> bool:
    design = context.last(DESIGN)
    return design is not None and (handoff.created_at or "") >= (design.created_at or "")


def _reported(output: dict[str, Any]) -> dict[str, Any]:
    """模型自报的部分，原样进交接(审查与交付读它)。"""
    keys = ("testsWritten", "verification", "deviations", "incidentalFindings", "outOfScope", "knowledgeSuggestions",
            "release", "bigIssue")
    return {key: output.get(key) for key in keys}


def _git(context: ImplementContext) -> Git:
    if context.git is None:
        raise ValueError(f"Issue {context.issue.id} 还没有修复 worktree")
    return context.git


def _base(context: ImplementContext) -> str:
    if context.base_commit is None:
        raise ValueError(f"Issue {context.issue.id} 没有基准 commit")
    return context.base_commit


def _handoff(runtime: Runtime, context: ImplementContext, status: Status, summary: str, facts: dict[str, Any],
             usage: Usage, *, notes: str | None = None, files_changed: int | None = None,
             lines_changed: int | None = None) -> Handoff:
    return Handoff(point=POINT, subject=context.issue.id, run=runtime.run, status=status, summary=summary,
                   facts=facts, notes=notes, round=context.round,
                   metrics=usage.metrics(files_changed=files_changed, lines_changed=lines_changed),
                   versions=usage.versions(runtime))


__all__ = ["corrections_for", "failing", "run", "tests_passed_in"]
