"""按问题来源确认：合并部署后，最初报出问题的地方不再报才算修好。

只做实施阶段做不到的：前面的检查都在合并前、在本机或测试环境，上线后在线上确认一次。每个关联问题读 store 的
problems 与 occurrences，不另去查平台(采集照常按自己的调度在跑，再出现会记成新的出现)：
- 部署之后又出现：回归(列出最近一次出现的时间与 commit)；
- 观察期满没再出现：通过；观察期内：等待，写明到期时间；
- 整体：有回归即回归；否则有等待即等待；否则通过。没有关联问题的 Issue 部署后即通过。
同一次部署的多个 Issue 由发布在一次运行内一起确认：部署记录读一次、fetch 至多一次(release/deploy.py)。
Issue 正文中只能在部署后确认的验收标准(观察类来源的「部署后的观察期内不再出现指纹为…的问题」，
assess/issue/body.post_deploy)是验收阶段的标准，实施不对应、不判断，在这里按指纹对应到关联问题的确认结论(`criteria`)。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from tightrein.assess.issue.body import post_deploy_fingerprint
from tightrein.protocol.naming import format_iso
from tightrein.release.accept.windows import window
from tightrein.settings.load import Settings
from tightrein.store.tables import occurrences, problems

PASSED, WAITING, REGRESSED = "passed", "waiting", "regressed"


@dataclass(frozen=True)
class ProblemCheck:
    problem: str
    source: str
    result: str
    detail: str
    due: str  # 观察期到期时刻
    fingerprint: str | None = None


@dataclass(frozen=True)
class Confirmation:
    result: str
    checks: tuple[ProblemCheck, ...] = field(default=())

    @property
    def regressions(self) -> tuple[ProblemCheck, ...]:
        return tuple(check for check in self.checks if check.result == REGRESSED)

    @property
    def due(self) -> str | None:
        waiting = [check.due for check in self.checks if check.result == WAITING]
        return max(waiting) if waiting else None

    def summary(self) -> str:
        if self.result == REGRESSED:
            return "部署后再次出现：" + "；".join(f"{check.problem} {check.detail}" for check in self.regressions)
        if self.result == WAITING:
            return f"观察期到 {self.due}"
        if not self.checks:
            return "没有关联问题，部署后即确认"
        return "观察期内没有再出现：" + "、".join(check.problem for check in self.checks)

    def to_json(self) -> dict[str, object]:
        return {"result": self.result, "due": self.due,
                "checks": [{"problem": check.problem, "source": check.source, "result": check.result,
                            "detail": check.detail, "due": check.due} for check in self.checks]}

    def criteria(self, items: list[str]) -> list[dict[str, str]]:
        """部署后才能确认的验收标准各自的结论：按标准中的指纹对应到关联问题；对应不上的(问题已并入别处)取整体结论。"""
        by_print = {check.fingerprint: check.result for check in self.checks if check.fingerprint}
        return [{"criterion": item, "result": by_print.get(post_deploy_fingerprint(item) or "", self.result)}
                for item in items]


def observe(problem: problems.Problem, conn: sqlite3.Connection, deployed_at: datetime, now: datetime,
            settings: Settings) -> ProblemCheck:
    due = deployed_at + window(problem.source, settings)
    later = [item for item in occurrences.find(conn, problem.id, since=deployed_at) if item.seen_at > deployed_at]
    if later:
        last = later[-1]
        commit = f"，commit {last.commit[:12]}" if last.commit else ""
        return ProblemCheck(problem.id, problem.source, REGRESSED,
                            f"部署后出现 {len(later)} 次(最近一次 {format_iso(last.seen_at)}{commit})", format_iso(due),
                            problem.fingerprint)
    if now >= due:
        return ProblemCheck(problem.id, problem.source, PASSED, "观察期内没有再出现", format_iso(due), problem.fingerprint)
    return ProblemCheck(problem.id, problem.source, WAITING, f"观察期到 {format_iso(due)}", format_iso(due),
                        problem.fingerprint)


def decide(checks: tuple[ProblemCheck, ...]) -> str:
    """回归优先，其次等待，否则通过。"""
    results = {check.result for check in checks}
    if REGRESSED in results:
        return REGRESSED
    return WAITING if WAITING in results else PASSED


def confirm(conn: sqlite3.Connection, problem_ids: list[str], deployed_at: datetime, now: datetime,
            settings: Settings) -> Confirmation:
    found = [problem for problem in (problems.get(conn, item) for item in problem_ids) if problem is not None]
    checks = tuple(observe(problem, conn, deployed_at, now, settings) for problem in found)
    return Confirmation(decide(checks), checks)
