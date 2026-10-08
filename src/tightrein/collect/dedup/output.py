"""输出：本次变成新发现或回归的问题各一份交给评估，加一份去重的运行交接与一份变更日志。

- 交给评估的只是本次变成 new 或 regressed、到最后仍是这个状态且没有被合并的问题；有 Issue 的问题再次出现不重复送，
  回归的才送，并带上 Issue 编号；
- 样本按 (角色, 位置) 去重，从最近的取，最多 SAMPLE_LIMIT 条：同一处的重复信号不占名额，不会挤掉其他角色或位置；
- 要写的文件先整体算好，与问题一起在同一个事务里记进 state 表(UNWRITTEN_KEY)，提交后再落盘、删掉记录；
  事务已提交、文件没写完就中断时，下次开始先按记录补写(recover)，交接文档不会缺；
- 变更日志(`19-collect.dedup-log.jsonl`)逐行记下本次写入的问题快照、出现与状态转换(连同转换所用的上下文)，
  从文件重建数据库时按运行顺序重放它，结果与增量处理一致，编号不变。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tightrein.collect.dedup.changes import ALIASES, MERGED_INTO, SOURCES, ChangeSet, Reproduction
from tightrein.collect.dedup.status import FOR_ASSESS, ROLES, ProblemStatus
from tightrein.protocol.handoff import Handoff, Metrics, Status
from tightrein.protocol.naming import Clock, FileName, format_iso
from tightrein.store.files.atomic import write_text
from tightrein.store.files.directories import run_dirs
from tightrein.store.files.json import dumps
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.rebuild import RebuildError
from tightrein.store.tables import occurrences, problems, state
from tightrein.store.tables.occurrences import Occurrence
from tightrein.store.tables.problems import Problem

POINT = "collect.dedup"
SAMPLE_LIMIT = 5
RECENT_RECORDS = 50  # 取样本时每个问题从数据库读的最近出现条数
UNWRITTEN_KEY = "collect.dedup.unwritten"  # {"run": 运行编号, "files": {相对工作区的路径: 内容}}
HANDOFF = FileName(POINT, "handoff", "json")
LOG = FileName(POINT, "log", "jsonl")
PROBLEM_RECORD, OCCURRENCE_RECORD, TRANSITION_RECORD = "problem", "occurrence", "transition"


@dataclass
class Summary:
    """本次去重的结论(运行交接的必填事实)。"""

    signals: int = 0
    suppressed: int = 0
    created: list[str] = field(default_factory=list)
    accumulated: int = 0
    merged: int = 0
    new: list[str] = field(default_factory=list)
    regressed: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    watching: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def for_assess(changeset: ChangeSet) -> list[tuple[Problem, str]]:
    """(问题, 本次最后转成的状态)，按编号排序。"""
    found = []
    for problem_id in sorted({item.problem for item in changeset.transitions}):
        problem = changeset.known[problem_id]
        became = changeset.became(problem_id)
        if became not in FOR_ASSESS or problem.status != became or problem.extra.get(MERGED_INTO) is not None:
            continue
        if became == ProblemStatus.NEW and problem.issue is not None:
            continue
        found.append((problem, became))
    return found


def summarize(changeset: ChangeSet, signals: int, suppressed: int, reopened: Sequence[str]) -> Summary:
    sent = for_assess(changeset)
    became = {problem_id: changeset.became(problem_id)
              for problem_id in {item.problem for item in changeset.transitions}}
    return Summary(
        signals=signals, suppressed=suppressed, created=list(changeset.created),
        accumulated=sum(1 for problem_id in changeset.occurred if problem_id not in changeset.created),
        merged=changeset.merged,
        new=[problem.id for problem, status in sent if status == ProblemStatus.NEW],
        regressed=[problem.id for problem, status in sent if status == ProblemStatus.REGRESSED],
        resolved=sorted(key for key, value in became.items() if value == ProblemStatus.RESOLVED),
        watching=sorted(key for key, value in became.items() if value == ProblemStatus.WATCHING),
        reopened=list(reopened), notes=list(changeset.notes),
    )


def recent_records(conn: sqlite3.Connection, problem_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """每个问题最近的出现记录(新到旧)，一次查完。"""
    wanted = sorted(set(problem_ids))
    if not wanted:
        return {}
    rows = conn.execute(
        f"SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY problem ORDER BY seen_at DESC, id DESC) AS rank "
        f"FROM occurrences WHERE problem IN ({', '.join('?' for _ in wanted)})) WHERE rank <= ? "
        "ORDER BY problem, seen_at DESC, id DESC", [*wanted, RECENT_RECORDS],
    ).fetchall()
    found: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        found.setdefault(row["problem"], []).append(_record(occurrences.TABLE.from_row(row)))
    return found


def files(layout: WorkspaceLayout, changeset: ChangeSet, summary: Summary, metrics: Metrics,
          stored: Mapping[str, list[dict[str, Any]]], clock: Clock) -> dict[str, str]:
    """本次要写的全部文件：{相对工作区的路径: 内容}。stored 为送评估问题在数据库中最近的出现记录。"""
    now = format_iso(clock.now())
    result: dict[str, str] = {}
    for problem, became in for_assess(changeset):
        records = [*(_record(item) for item in reversed(_occurrences_of(changeset, problem.id))),
                   *stored.get(problem.id, [])]
        handoff = problem_handoff(changeset, problem, became, records, now)
        result[_relative(layout, layout.step_file(problem.id, HANDOFF))] = dumps(handoff.to_json())
    run_handoff = Handoff(
        point=POINT, subject=changeset.run, run=changeset.run, status=Status.PASSED, summary=_headline(summary),
        facts=asdict(summary) | {"forAssess": [*summary.new, *summary.regressed]}, metrics=metrics, created_at=now,
    )
    result[_relative(layout, layout.step_file(changeset.run, HANDOFF))] = dumps(_camel_facts(run_handoff))
    result[_relative(layout, layout.step_file(changeset.run, LOG))] = "".join(
        json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n" for line in log_lines(changeset))
    return result


def problem_handoff(changeset: ChangeSet, problem: Problem, became: str, records: Sequence[dict[str, Any]],
                    now: str) -> Handoff:
    """records 为该问题的出现记录，新到旧(忽略按日期到期而本次没有出现的，可能为空)。"""
    transition = next(item for item in reversed(changeset.transitions)
                      if item.problem == problem.id and item.after == became)
    latest = records[0] if records else None
    reproduction = changeset.reproduction.get(problem.id)
    facts = {
        "problem": problem_facts(problem),
        "transition": {"event": transition.event, "before": transition.before, "after": transition.after,
                       "context": transition.context, "at": transition.at},
        "latest": latest,
        "samples": samples(records[1:], latest) if latest is not None else [],
        "reproduction": _reproduction(reproduction),
        "regressionCheck": problem.id in changeset.regression_checks,  # 本次是复现检查(回归信号)失败
        "issue": problem.issue if became == ProblemStatus.REGRESSED else None,
    }
    return Handoff(point=POINT, subject=problem.id, run=changeset.run, status=Status.PASSED,
                   summary=f"{problem.title}（{became}）", facts=facts, created_at=now)


def samples(records: Sequence[Mapping[str, Any]], latest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按 (角色, 位置) 去重，从最近的取，最多 SAMPLE_LIMIT 条；与最近一条同一处的不再取。"""
    seen = {_sample_key(latest)}
    found = []
    for record in records:
        key = _sample_key(record)
        if key in seen:
            continue
        seen.add(key)
        found.append(dict(record))
        if len(found) == SAMPLE_LIMIT:
            break
    return found


def problem_facts(problem: Problem) -> dict[str, Any]:
    return {
        "id": problem.id, "fingerprint": problem.fingerprint, "source": problem.source,
        "sources": problem.extra.get(SOURCES, [problem.source]), "checkType": problem.check_type,
        "status": problem.status, "title": problem.title, "location": problem.location,
        "firstSeen": format_iso(problem.first_seen), "lastSeen": format_iso(problem.last_seen),
        "count": problem.count, "lastCommit": problem.last_commit, "issue": problem.issue,
        "aliases": problem.extra.get(ALIASES, []), "roles": problem.extra.get(ROLES, []),
    }


def log_lines(changeset: ChangeSet) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = [
        {"kind": PROBLEM_RECORD, "row": encode(problems.TABLE, changeset.known[problem_id])}
        for problem_id in sorted(changeset.touched)
    ]
    lines += [{"kind": OCCURRENCE_RECORD, "row": encode(occurrences.TABLE, item)} for item in changeset.occurrences]
    lines += [{"kind": TRANSITION_RECORD, **asdict(item)} for item in changeset.transitions]
    return lines


def encode(table: Any, record: Any) -> dict[str, Any]:
    """按列编码(时间为 ISO 文本，JSON 列保持结构)，重建时用 decode 还原。"""
    return {column: (getattr(record, column) if column in table.json_columns
                     else table.encode(column, getattr(record, column))) for column in table.columns}


def decode(table: Any, row: Mapping[str, Any]) -> Any:
    return table.record(**{column: (row.get(column) if column in table.json_columns
                                    else table.decode(column, row.get(column))) for column in table.columns})


def remember(conn: sqlite3.Connection, run: str, written: Mapping[str, str], clock: Clock) -> None:
    """在去重的事务里记下要写的文件(与问题同时提交)。"""
    state.put(conn, UNWRITTEN_KEY, {"run": run, "files": dict(written)}, clock)


def flush(layout: WorkspaceLayout, conn: sqlite3.Connection) -> str | None:
    """把记下的文件落盘并删掉记录；返回补写的运行编号，没有要写的为 None。中断恢复与正常结束都走这里。"""
    pending = state.get(conn, UNWRITTEN_KEY)
    if pending is None:
        return None
    for relative, text in pending["files"].items():
        write_text(layout.root / relative, text)
    state.delete(conn, UNWRITTEN_KEY)
    return str(pending["run"])


def rebuild_problems(layout: WorkspaceLayout, conn: sqlite3.Connection) -> int:
    """从各运行目录的变更日志重建 problems 与 occurrences(按运行编号即时间顺序重放)：问题取最后一份快照，
    出现逐条写回。返回写回的问题数。日志写坏的一次列出全部，整体回滚。"""
    snapshots: dict[str, Problem] = {}
    found: list[Occurrence] = []
    errors: list[str] = []
    for directory, _ in run_dirs(layout):
        path = directory / LOG.render()
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record["kind"] == PROBLEM_RECORD:
                    problem = decode(problems.TABLE, record["row"])
                    snapshots[problem.id] = problem
                elif record["kind"] == OCCURRENCE_RECORD:
                    found.append(decode(occurrences.TABLE, record["row"]))
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"{path}：{error}")
    if errors:
        raise RebuildError("problems", errors)
    for problem_id in sorted(snapshots):
        problems.TABLE.insert(conn, snapshots[problem_id], snapshots[problem_id].first_seen)
    for item in found:
        occurrences.TABLE.insert(conn, item, item.seen_at)
    return len(snapshots)


def _occurrences_of(changeset: ChangeSet, problem_id: str) -> list[Occurrence]:
    return [item for item in changeset.occurrences if item.problem == problem_id]


def _record(item: Occurrence) -> dict[str, Any]:
    return {**item.evidence, "occurredAt": format_iso(item.seen_at), "source": item.source, "run": item.run,
            "commit": item.commit}


def _sample_key(record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    evidence = record.get("evidence") or {}
    return evidence.get("role"), record.get("location")


def _reproduction(found: Reproduction | None) -> dict[str, Any] | None:
    return None if found is None else {"strategy": found.strategy, "attempts": found.attempts, "result": found.result}


def _headline(summary: Summary) -> str:
    return (f"处理 {summary.signals} 条信号：新问题 {len(summary.created)}，累计 {summary.accumulated}，"
            f"抑制 {summary.suppressed}，送评估 {len(summary.new) + len(summary.regressed)}"
            f"(回归 {len(summary.regressed)})，判为已解决 {len(summary.resolved)}")


def _camel_facts(handoff: Handoff) -> dict[str, Any]:
    """运行交接的事实键与其余部分一样用小驼峰。"""
    data = handoff.to_json()
    data["facts"] = {_camel(key): value for key, value in data["facts"].items()}
    return data


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _relative(layout: WorkspaceLayout, path: Path) -> str:
    return str(path.relative_to(layout.root))


