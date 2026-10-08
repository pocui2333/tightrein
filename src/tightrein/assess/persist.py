"""落库：评估结论写进问题(problems.extra.assess 与状态)，以及 Issue 关闭、复现不了时对关联问题的连带处理。

- 一个问题的落库在一个事务内完成(结论、问题状态、抑制规则、并入、写成 Issue)，失败全部回滚，问题视为没处理过；
  并入其他问题时不写评估结论；
- 问题状态的去向：要修的 → ongoing(带 Issue 编号)；证据不足 → watching(再出现时由采集提升为 new，带新证据重新
  评估)；观察 → muted(忽略到再出现 N 次或严重度升级)；不成立、不修、取舍、并入 → closed(不成立另生成抑制规则)；
  主干上已修 → ongoing(等部署后由采集判为已解决)；转人工 → 保持原状态，带 manual 标记；
- 评估对问题的每次改动另把整行快照写进问题目录的 `00-problem-assess.json`(带运行编号)：从文件重建时，采集重放
  它的变更日志之后，比该问题最后一份采集快照更新的评估快照覆盖上去(replay)；
- 判定与严重度另写进 problems.extra 的 verdict、severity(status、watch 读取)；
- 误判的数据来源：Issue 以「不是缺陷」关闭、修复前复现不了记为 false_confirm(误判为成立)，用户把「不成立」
  改判为成立记为 false_refute(误判为不成立)，验收通过记为 correct；已有结果的不覆盖。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.assess.select import ASSESS, MERGED_INTO
from tightrein.protocol.naming import FileName, format_iso, local_date, run_started
from tightrein.store.files.directories import run_dirs, subdirectories
from tightrein.store.files.json import read_json, write_json
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import problems
from tightrein.store.tables.problems import Problem

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime
    from tightrein.store.tables.issues import Issue

POINT = "assess.triage"
SNAPSHOT = "assess"  # 00-problem-assess.json
FALSE_CONFIRM = "false_confirm"
FALSE_REFUTE = "false_refute"
CORRECT = "correct"
OVERRIDDEN = "overridden"
ALIASES = "aliases"
# Issue 关闭原因 → 关联问题的去向
IGNORE_UNTIL_CHANGE = frozenset({"wont_fix", "fix_rejected"})


def update(runtime: Runtime, problem: Problem, *, status: str | None = None, assess: Mapping[str, Any] | None = None,
           **fields: Any) -> Problem:
    """改问题的状态、评估记录与其余列，写库并记一行快照；调用方负责事务。"""
    extra = dict(problem.extra)
    if assess is not None:
        extra[ASSESS] = {**(extra.get(ASSESS) or {}), **assess}
        extra.update({key: assess[key] for key in ("verdict", "severity") if key in assess})
    changed = replace(problem, status=status or problem.status, extra=extra, **fields)
    problems.save(runtime.conn, changed, runtime.clock)
    snapshot(runtime.workspace, runtime.run, changed)
    return changed


def snapshot(layout: WorkspaceLayout, run: str, problem: Problem) -> None:
    """整行快照写进问题目录(重建时用)；格式与采集的变更日志中的问题快照相同。"""
    from tightrein.collect.dedup.output import encode

    write_json(snapshot_path(layout, problem.id), {"run": run, "row": encode(problems.TABLE, problem)})


def snapshot_path(layout: WorkspaceLayout, problem_id: str) -> Path:
    return layout.shared_file(problem_id, SNAPSHOT, "json")


def touched_paths(runtime: Runtime, problem_ids: Sequence[str]) -> list[Path]:
    """一个事务里评估可能改写的问题文件：评估快照与本次评估结论的误判交接(回填时写)。"""
    paths: list[Path] = []
    for problem_id in problem_ids:
        paths.append(snapshot_path(runtime.workspace, problem_id))
        found = problems.get(runtime.conn, problem_id)
        if found is not None and found.extra.get(ASSESS):
            paths.append(misjudged_path(runtime, problem_id, int(found.extra[ASSESS].get("attempt") or 1)))
    return paths


@contextmanager
def restoring(runtime: Runtime, problem_ids: Sequence[str]) -> Iterator[None]:
    """事务回滚时问题文件也回到原样，从文件重建时不会重放没提交的改动。"""
    from tightrein.assess.issue.files import restoring_paths

    with restoring_paths(touched_paths(runtime, problem_ids)):
        yield


def replay(layout: WorkspaceLayout, conn: sqlite3.Connection) -> int:
    """从文件重建时，在采集重放完变更日志之后调用：评估快照比该问题最后一份采集快照新的，整行覆盖。返回覆盖的个数。
    快照写坏的一次列出全部，整体回滚(与采集的重放一致)。"""
    from tightrein.collect.dedup.output import LOG, PROBLEM_RECORD, decode
    from tightrein.store.rebuild import RebuildError

    collected: dict[str, str] = {}
    for directory, _ in run_dirs(layout):
        path = directory / LOG.render()
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("kind") == PROBLEM_RECORD:
                    collected[record["row"]["id"]] = directory.name
    chosen: list[tuple[Problem, str]] = []
    errors: list[str] = []
    for directory in subdirectories(layout.problems_dir):
        path = snapshot_path(layout, directory.name)
        if not path.is_file():
            continue
        try:
            data = read_json(path)
            latest = collected.get(directory.name)
            if latest is not None and run_started(latest) > run_started(data["run"]):
                continue
            chosen.append((decode(problems.TABLE, data["row"]), str(data["run"])))
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"{path}：{error}")
    if errors:
        raise RebuildError("problems", errors)
    for row, run in chosen:
        problems.TABLE.save(conn, row, run_started(run))
    return len(chosen)


def suppress(runtime: Runtime, problem: Problem, reason: str) -> None:
    """判为不成立：按指纹(含别名)生成带到期日的抑制规则，被抑制的信号不再关联到问题。"""
    from tightrein.collect.dedup.suppress import SuppressionRule, add

    days = int(runtime.settings.section("assess")["suppressionDays"])
    today = local_date(runtime.clock.now())
    for fingerprint in (problem.fingerprint, *problem.extra.get(ALIASES, [])):
        add(runtime.conn, SuppressionRule.for_fingerprint(fingerprint, reason, today, days), runtime.clock)


def mute(runtime: Runtime, problem: Problem, *, occurrences: int | None, new_release: bool,
         assess: Mapping[str, Any] | None = None) -> Problem:
    """忽略到再出现 N 次、出现在新版本或严重度升级(任一满足即由采集恢复为 new)。"""
    from tightrein.collect.dedup.status import IGNORE, ignore_condition

    condition = ignore_condition(problem, occurrences=occurrences, new_release=new_release, severity_escalated=True)
    problem = replace(problem, extra={**problem.extra, IGNORE: condition.to_json()})
    return update(runtime, problem, status="muted", assess=assess)


def merge(runtime: Runtime, problem: Problem, target: str, reason: str) -> Problem:
    """并入同一根因的问题：本问题关闭并记下并入目标，指纹作为目标的别名，之后的信号归到目标上。"""
    other = problems.get(runtime.conn, target)
    if other is None:
        raise LookupError(f"并入目标 {target} 不存在")
    aliases = list(other.extra.get(ALIASES, []))
    for value in (problem.fingerprint, *problem.extra.get(ALIASES, [])):
        if value != other.fingerprint and value not in aliases:
            aliases.append(value)
    update(runtime, replace(other, extra={**other.extra, ALIASES: aliases}))
    merged = replace(problem, extra={**problem.extra, MERGED_INTO: target})
    return update(runtime, merged, status="closed", assess={"mergedInto": target, "mergeReason": reason,
                                                            "at": format_iso(runtime.clock.now()), "retriage": False})


def close_problems(runtime: Runtime, issue: Issue, problem_ids: Sequence[str], close_reason: str, *,
                   actor: str) -> None:
    """Issue 关闭时的连带处理：不修与修复未采纳的问题忽略到严重度升级或出现在新版本；不是缺陷的问题关闭并生成
    抑制规则；重复的问题改挂到另一个 Issue。"""
    from tightrein.assess.issue import files
    from tightrein.store.tables import issues

    reason = f"Issue {issue.id} 以 {close_reason} 关闭(操作者 {actor})"
    found = [problem for problem in (problems.get(runtime.conn, pid) for pid in problem_ids) if problem is not None]
    if close_reason in IGNORE_UNTIL_CHANGE:
        for problem in found:
            mute(runtime, problem, occurrences=None, new_release=True)
    elif close_reason == "not_a_bug":
        for problem in found:
            suppress(runtime, problem, reason)
            update(runtime, problem, status="closed")
    elif close_reason == "duplicate":
        target_id = str(issue.extra["duplicateOf"])
        target = issues.get(runtime.conn, target_id)
        if target is None:
            raise LookupError(f"重复关闭指向的 Issue {target_id} 不存在")
        members = list(target.extra.get("problems") or [])
        moved = [problem.id for problem in found if problem.id not in members]
        target.extra["problems"] = members + moved
        target.extra.setdefault("history", []).append({
            "at": format_iso(runtime.clock.now()), "event": "append", "actor": actor,
            "reason": None, "note": f"Issue {issue.id} 以重复关闭，并入问题 {'、'.join(moved) or '无'}"})
        files.write(runtime, target)
        for problem in found:
            update(runtime, problem, issue=target_id)


def fill_outcome(runtime: Runtime, problem_ids: Sequence[str], outcome: str, detail: str) -> None:
    """回填最近一次评估结论的实际结果；已有结果的不覆盖(由更早的一条负责，不重复计数)。
    误判另写一份交接(必填事实 misjudged)，复盘据此统计。"""
    for problem_id in problem_ids:
        problem = problems.get(runtime.conn, problem_id)
        if problem is None or not problem.extra.get(ASSESS) or problem.extra[ASSESS].get("outcome"):
            continue
        update(runtime, problem, assess={"outcome": outcome, "outcomeAt": format_iso(runtime.clock.now())})
        if outcome in (FALSE_CONFIRM, FALSE_REFUTE):
            misjudged_handoff(runtime, problem_id, int(problem.extra[ASSESS].get("attempt") or 1),
                              {"kind": outcome, "point": POINT, "detail": detail})


def misjudged_handoff(runtime: Runtime, problem_id: str, attempt: int, misjudged: Mapping[str, str]) -> None:
    """第几次评估被证明误判：写在问题目录 `21-assess.triage.r<次>-handoff.json`(每次评估最多回填一次，不会覆盖)。"""
    from tightrein.protocol.handoff import Handoff, Status, write

    write(misjudged_path(runtime, problem_id, attempt), Handoff(
        point=POINT, subject=problem_id, run=runtime.run, status=Status.PASSED,
        summary=f"第 {attempt} 次评估回填为 {misjudged['kind']}：{misjudged['detail']}",
        facts={"problem": problem_id, "attempt": attempt, "misjudged": dict(misjudged), "knowledgeSuggestions": []},
        round=attempt, created_at=format_iso(runtime.clock.now())))


def misjudged_path(runtime: Runtime, problem_id: str, attempt: int) -> Path:
    return runtime.workspace.step_file(problem_id, FileName(POINT, "handoff", "json", round=attempt))


def request_retriage(runtime: Runtime, problem_ids: Sequence[str], note: str) -> None:
    for problem_id in problem_ids:
        problem = problems.get(runtime.conn, problem_id)
        if problem is None:
            continue
        record = problem.extra.get(ASSESS) or {}
        update(runtime, problem, assess={"retriage": True, "manual": False,
                                         "retriageNotes": [*record.get("retriageNotes", []), note]})
