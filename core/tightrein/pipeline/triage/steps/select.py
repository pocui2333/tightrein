"""选取待分诊的问题并排序(architecture/06 3.2、3.3)。

可分诊的条件：没有被合并；状态为 new 或 regressed 且进入该状态之后没有分诊结论；或者状态为 new、ongoing、regressed
且有未处理的 retriage-requested 事件。ignored 的问题需要先 reopen。
排序按五档：预估 P0(越权检查失败)、运行报错(api-fuzz 的服务端错误、内部错误)、回归、其他运行时
问题、static 与 incidental 以及预估为 P3 的问题(响应过慢)；同一档内按末次出现时间倒序。预估只用于排序。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from tightrein.domain.enums import Probe, ProblemEvent, ProblemStatus
from tightrein.domain.fingerprint import AUTHZ_CHECK
from tightrein.domain.problem import Problem
from tightrein.domain.signal import Signal
from tightrein.store.repos import problem_events, problems, signals, triage

TRIAGEABLE = frozenset({ProblemStatus.NEW, ProblemStatus.ONGOING, ProblemStatus.REGRESSED})
FOR_TRIAGE = frozenset({ProblemStatus.NEW, ProblemStatus.REGRESSED})
SERVER_ERROR_CHECK = "not_a_server_error"
SLOW_CHECK = "max_response_time"
ALREADY_TRIAGED = "已分诊，重新分诊请用 tightrein problem retriage"
NOT_TRIAGEABLE = "状态为 {status}，只能分诊新发现、持续或回归的问题；已忽略的问题先执行 tightrein problem reopen"


@dataclass(frozen=True)
class Selection:
    chosen: list[Problem] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


def latest_signal(conn: sqlite3.Connection, problem_id: str) -> Signal | None:
    found = signals.get_many(conn, problems.signal_ids(conn, problem_id))
    return max(found, key=lambda signal: (signal.occurred_at, signal.id)) if found else None


def estimated_p0(problem: Problem, latest: Signal | None) -> bool:
    return problem.probe is Probe.API_FUZZ and latest is not None and latest.check == AUTHZ_CHECK


def _server_error(problem: Problem, latest: Signal | None) -> bool:
    if problem.probe is Probe.PLATFORM_ERRORS:
        return True
    return problem.probe is Probe.API_FUZZ and latest is not None and latest.check == SERVER_ERROR_CHECK


def band(problem: Problem, latest: Signal | None) -> int:
    if estimated_p0(problem, latest):
        return 0
    if _server_error(problem, latest):
        return 1
    if problem.status is ProblemStatus.REGRESSED:
        return 2
    if problem.probe in (Probe.STATIC, Probe.INCIDENTAL) or (latest is not None and latest.check == SLOW_CHECK):
        return 4
    return 3


def _entered_at(conn: sqlite3.Connection, problem: Problem) -> datetime | None:
    entered = [record.at for record in problem_events.for_problem(conn, problem.id)
               if record.to_status is problem.status and record.from_status is not problem.status]
    return entered[-1] if entered else None


def retriage_requests(conn: sqlite3.Connection, problem_id: str) -> list[int]:
    return [record.id for record in problem_events.unhandled(conn, ProblemEvent.RETRIAGE_REQUESTED)
            if record.problem_id == problem_id and record.id is not None]


def eligible(conn: sqlite3.Connection, problem: Problem) -> bool:
    if problem.merged_into is not None or problem.status not in TRIAGEABLE:
        return False
    if retriage_requests(conn, problem.id):
        return True
    if problem.status not in FOR_TRIAGE:
        return False
    latest = triage.latest(conn, problem.id)
    entered = _entered_at(conn, problem)
    return latest is None or (entered is not None and latest.created_at < entered)


def order(conn: sqlite3.Connection, found: list[Problem]) -> list[Problem]:
    keyed = [(band(problem, latest_signal(conn, problem.id)), -problem.last_seen_at.timestamp(), problem.id, problem)
             for problem in found]
    return [item[-1] for item in sorted(keyed, key=lambda item: item[:3])]


def pending(conn: sqlite3.Connection) -> list[Problem]:
    return order(conn, [problem for problem in problems.find(conn, statuses=TRIAGEABLE) if eligible(conn, problem)])


def rejection(conn: sqlite3.Connection, problem_id: str, *, retriage: bool = False,
              ignore_state: bool = False) -> tuple[Problem | None, str | None]:
    """指定编号的问题能否分诊：返回问题与不能分诊的原因。retriage 时不看是否已分诊；ignore_state(只用于 --output)
    时不看状态。"""
    problem = problems.get(conn, problem_id)
    if problem is None:
        return None, f"{problem_id} 不存在"
    if problem.merged_into is not None:
        return problem, f"{problem_id} 已并入 {problem.merged_into}"
    if ignore_state:
        return problem, None
    if problem.status not in TRIAGEABLE:
        return problem, f"{problem_id} " + NOT_TRIAGEABLE.format(status=problem.status.label)
    if not retriage and not eligible(conn, problem):
        return problem, f"{problem_id} {ALREADY_TRIAGED}"
    return problem, None


def choose(conn: sqlite3.Connection, limit: int, problem_ids: tuple[str, ...] = (), *, retriage: bool = False,
           ignore_state: bool = False) -> Selection:
    if not problem_ids:
        return Selection(pending(conn)[:limit])
    selection = Selection()
    for problem_id in problem_ids:
        problem, reason = rejection(conn, problem_id, retriage=retriage, ignore_state=ignore_state)
        if problem is None or reason is not None:
            selection.rejected.append((problem_id, reason or f"{problem_id} 不存在"))
        else:
            selection.chosen.append(problem)
    selection.chosen[:] = order(conn, selection.chosen)[:limit]
    return selection
