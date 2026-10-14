"""调度(protocol/schedule.md)：一次运行按状态把对象往前推，顺序为 采集 → 评估 → 实施 → 发布 → 复盘。

调度只看对象处于什么状态，不判断对象该怎么处理(那是各阶段的事)：
1. 暂停、急停或额度用完时不开始(退出码 3)；定时触发只跑就绪的项目、只在能跑的时段；
2. 运行锁不等待：上一次没跑完就跳过，定时与事件触发记一条「跳过」；
3. 启动恢复(心跳失效或进程已不在的运行标为中断) → 保留期清理；
4. 各阶段依次推进，每一步(每个对象的每一步)包在独立的错误边界里：一步出错只记为该步失败，其余照常；
5. 每开始一步、一个新对象前查暂停：暂停在当前这一步做完后生效；
6. 额度到了留余量的门槛时不开始新的对象(评估新问题、开始新 Issue)，手上的照常做完；
7. 实施缺省同时只处理一个 Issue：有进行中的先做它，没有才按严重度开始一个新的；并行时会改到同一文件的不同时做；
   还在等前置 Issue 合入的、停在关卡等用户而还没有新决定的不挑；
8. 对象熔断：同一对象连续失败或推进后没有前进，达到次数即不再处理、交给人(手动接管)；停在人工关卡不计数；
9. 每次运行结束都执行复盘。
运行期间后台线程每隔 limits.lock.heartbeat 续运行锁与 runs 表的心跳：一步可能跑半小时，心跳停了别的进程会判它中断。
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo
from enum import StrEnum
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any

from tightrein.assess.issue import blockers
from tightrein.assess.issue.stages import IMPLEMENT, RELEASE, stage_for, statuses_for
from tightrein.onboard.check import READY_KEY
from tightrein.protocol import recovery
from tightrein.protocol.git.worktrees import ReadonlyRestoreFailed, recover_readonly
from tightrein.protocol.handoff import Status, read
from tightrein.protocol.naming import STAGES, FileName, format_iso, kind_of, parse_duration, parse_iso
from tightrein.protocol.records import EventLog
from tightrein.protocol.runtime import Runtime, isolated
from tightrein.protocol.schedule.launchd import log_paths, rotate
from tightrein.settings.load import Settings
from tightrein.store import retention
from tightrein.store.db import connect
from tightrein.store.locks import Busy, FileLock, Lost, process_alive
from tightrein.store.tables import counters, issues, problems, runs, state

POINT = "protocol.schedule"
TRIGGERS = runs.TRIGGERS
MANUAL = "manual"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LOOKAHEAD_S = 8 * 86400  # 推算下次时刻最多往后看一周多
WAKE_KEY = "schedule.lastWake"  # state 表：上次定时醒来的时间
MISSED_KEY = "schedule.missed"  # state 表：上次定时醒来之前漏掉的醒来次数(机器睡眠、定时器没装好)
AUTOMATIC = frozenset({"schedule", "event"})
REFILL_POLL_S = 5.0  # 并行实施时多久看一次空位
REFILL_FACTOR = 2  # resources.refillMax 为 null 时：一轮最多开始 sessions 的这么多倍个 Issue
RUN_ORDER = ("implement", "release", "collect", "assess", "retro")  # 一轮之内的先后(见 _stages)
FOR_ASSESS = ("new", "regressed")
TODO, IMPLEMENTING, ACCEPTING = "todo", "implementing", "accepting"
FOR_IMPLEMENT = statuses_for(IMPLEMENT)  # 状态 → 阶段只在 assess/issue/stages.py 定义
FOR_RELEASE = statuses_for(RELEASE)
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
# 一次运行中同一对象最多推进的步数：实施 8 个小步骤 × 修正轮次再留余地；防止状态不前进时空转
MAX_STEPS = 40
BREAKER_PREFIX = "breaker.object."  # protocol/limits.py 中对象熔断计数的键
BREAKER_HOLDER = "tightrein:breaker"  # 熔断后以这个名字接管，`tightrein give` 交还
PLANNED_FILES = "plannedFiles"  # issues.extra：评估预估要改的文件(assess/issue/create.py 写入)
PREPARE_POINT, DESIGN_POINT = "implement.prepare", "implement.design"  # 读它们的交接判断 Issue 会改到哪些文件


class StepStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"  # 停在人工关卡
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # 不在时段、未就绪、运行锁被占
    REFUSED = "refused"  # 暂停、急停、额度停机(退出码 3)


@dataclass(frozen=True)
class Step:
    stage: str
    subject: str | None
    status: StepStatus
    summary: str


@dataclass(frozen=True)
class Planned:
    """--dry-run 时列出的会做的事。"""

    stage: str
    subject: str | None
    reason: str


@dataclass
class Outcome:
    run: str
    status: RunStatus
    reason: str | None = None
    steps: list[Step] = field(default_factory=list)
    planned: list[Planned] = field(default_factory=list)
    halted: str | None = None  # 中途停下的原因(暂停、急停、锁被接管)

    @property
    def pending(self) -> bool:
        return any(step.status is StepStatus.PENDING for step in self.steps)

    @property
    def failed(self) -> bool:
        return any(step.status is StepStatus.FAILED for step in self.steps)


def run(runtime: Runtime, *, trigger: str, stage: str | None = None, subject: str | None = None,
        dry_run: bool = False, host: str | None = None, alive: Callable[[int], bool] = process_alive) -> Outcome:
    if trigger not in TRIGGERS:
        raise ValueError(f"触发方式只能是 {'、'.join(TRIGGERS)}：{trigger}")
    if stage is not None and stage not in STAGES:
        raise ValueError(f"阶段只能是 {'、'.join(STAGES)}：{stage}")
    if subject is not None:
        _check_subject(runtime, subject)
    refused = _refusal(runtime)
    if refused is not None:
        return Outcome(runtime.run, RunStatus.REFUSED, refused)
    if trigger == "schedule":
        _note_wake(runtime)
    if trigger in AUTOMATIC:
        skipped = _not_now(runtime, trigger)
        if skipped is not None:
            return Outcome(runtime.run, RunStatus.SKIPPED, skipped)
    stages = _stages(runtime, trigger, stage, subject)
    if dry_run:
        return Outcome(runtime.run, RunStatus.DONE, planned=_plan(runtime, stages, subject, trigger))
    return _locked(runtime, trigger, stages, subject, host or socket.gethostname(), alive)


def object_tripped(runtime: Runtime, subject: str) -> bool:
    """对象熔断是否已打开(只读，不计数)。"""
    limit = int(runtime.settings.get("limits.breaker.objectFailures"))
    return counters.get(runtime.conn, BREAKER_PREFIX + subject) >= limit


def in_window(settings: Settings, now: datetime, zone: tzinfo | None = None) -> bool:
    """是否在能跑的时段与星期(本机时区)；from 晚于 to 时跨午夜(算在开始那天)。"""
    window = settings.get("schedule.window")
    local = now.astimezone(zone)
    start, end, moment = time.fromisoformat(window["from"]), time.fromisoformat(window["to"]), local.time()
    if start <= end:
        return WEEKDAYS[local.weekday()] in window["days"] and start <= moment < end
    day = local.weekday() if moment >= start else (local.weekday() - 1) % len(WEEKDAYS)
    return WEEKDAYS[day] in window["days"] and (moment >= start or moment < end)


def next_run(settings: Settings, now: datetime, zone: tzinfo | None = None) -> datetime | None:
    """下一次定时醒来且在能跑的时段内的时刻：醒来时刻按 schedule.tick 从本地零点对齐(与 launchd 的设置一致)。"""
    tick = settings.duration("schedule.tick")
    if tick <= 0:
        return None
    local = now.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = midnight + timedelta(seconds=(int((local - midnight).total_seconds() // tick) + 1) * tick)
    for _ in range(int(LOOKAHEAD_S // tick)):
        if in_window(settings, candidate, zone):
            return candidate
        candidate += timedelta(seconds=tick)
    return None


__all__ = ["MISSED_KEY", "Outcome", "Planned", "RunStatus", "Step", "StepStatus", "in_window", "next_run",
           "object_tripped", "run"]


# 运行


def _locked(runtime: Runtime, trigger: str, stages: list[str], subject: str | None, host: str,
            alive: Callable[[int], bool]) -> Outcome:
    lock = FileLock(runtime.workspace.run_lock, runtime.clock, stale_s=runtime.settings.duration("limits.lock.stale"))
    try:
        previous = lock.acquire(wait=False)
    except Busy as busy:
        return _skipped_busy(runtime, trigger, stages, str(busy))
    heartbeat = _Heartbeat(lock, runtime)
    outcome = Outcome(runtime.run, RunStatus.DONE)
    runs.start(runtime.conn, runs.Run(id=runtime.run, stage=_run_stage(stages), trigger=trigger, status="running",
                                      started_at=runtime.clock.now()))
    try:
        heartbeat.start()
        if previous is not None:
            _emit(runtime, None, "decision", f"接管失效的运行锁(进程 {previous.get('pid')}，主机 {previous.get('host')})")
        _prepare(runtime, trigger, host, alive)
        _advance(runtime, stages, subject, outcome, heartbeat)
    finally:
        heartbeat.stop()
        lock.release()
    outcome.status = RunStatus.FAILED if outcome.failed else RunStatus.DONE
    runs.finish(runtime.conn, runtime.run, outcome.status.value, runtime.clock, summary=_summary(outcome))
    return outcome


def _prepare(runtime: Runtime, trigger: str, host: str, alive: Callable[[int], bool]) -> None:
    """启动恢复与保留期清理；定时触发时顺带轮转 launchd 的日志。"""
    closed = recovery.recover(runtime.conn, runtime.clock, runtime.settings, host=host, alive=alive,
                              events_for=lambda run: EventLog(runtime.workspace.events(run), runtime.redactor,
                                                              runtime.clock), current=runtime.run)
    if closed:
        _emit(runtime, None, "decision", f"启动恢复：{len(closed)} 个运行标为中断")
    _restore_readonly(runtime, alive)
    policy = {kind: parse_duration(value) for kind, value in runtime.settings.get("records.retention").items()}
    removed = retention.purge(runtime.workspace, runtime.conn, runtime.clock, policy)
    if any(removed.values()):
        _emit(runtime, None, "effect", "保留期清理：" + "，".join(f"{kind} {count}" for kind, count in removed.items()))
    if trigger == "schedule":
        logs = runtime.settings.get("schedule.logs")
        for path in log_paths(runtime.workspace):
            rotate(path, max_bytes=int(logs["maxBytes"]), keep=int(logs["keep"]))


def _restore_readonly(runtime: Runtime, alive: Callable[[int], bool]) -> None:
    """持有进程已不在的只读锁定(chmod 去掉了写权限)恢复写权限；恢复失败保留标记，下次启动再试，不挡住本次运行。"""
    try:
        restored = recover_readonly(runtime.workspace.worktrees_dir, alive=alive)
    except ReadonlyRestoreFailed as error:
        _emit(runtime, None, "effect", f"启动恢复：{error}")
        return
    if restored:
        _emit(runtime, None, "effect", "启动恢复：恢复只读 worktree 的写权限 "
              + "、".join(str(item.worktree) for item in restored))


def _advance(runtime: Runtime, stages: list[str], subject: str | None, outcome: Outcome,
             heartbeat: _Heartbeat) -> None:
    steps = {"collect": _collect, "assess": _assess, "implement": _implement, "release": _release, "retro": _retro}
    for name in stages:
        halted = _halted(runtime, heartbeat)
        if halted is not None:
            outcome.halted = halted
            _emit(runtime, None, "decision", f"停下：{halted}")
            return
        steps[name](runtime, subject, outcome, heartbeat)


def _collect(runtime: Runtime, subject: str | None, outcome: Outcome, heartbeat: _Heartbeat) -> None:
    from tightrein.collect import collect  # 各阶段延迟导入：调度在协议层，不在导入时依赖各阶段

    # 到点判断在采集里(上次时间与标记随读取位置一起保存)；手动触发不看是否到点，启用的来源全部跑
    only = collect.sources(runtime) if _trigger(runtime) == MANUAL else None
    _guarded(runtime, outcome, "collect", None, lambda: collect.run(runtime, only=only),
             lambda result: (StepStatus.PASSED, _collected(result)))


def _assess(runtime: Runtime, subject: str | None, outcome: Outcome, heartbeat: _Heartbeat) -> None:
    from tightrein.assess import assess as assessing

    if subject is not None and kind_of(subject) != "problem":
        return
    if subject is not None:
        if _skip_object(runtime, subject, new=True) is None:
            _object_step(runtime, outcome, "assess", subject, lambda: assessing.assess(runtime, subject), _assessed)
        return
    if runtime.agents.quota.reserve_reached():
        outcome.steps.append(Step("assess", None, StepStatus.SKIPPED, _reserve(runtime)))
        return
    results = _guarded(runtime, outcome, "assess", None, lambda: assessing.assess_pending(runtime), None)
    for result in results or []:
        status = _step_status(result.status)
        outcome.steps.append(Step("assess", result.problem, status, _assessed(result)))
        if status is StepStatus.FAILED:
            _failed(runtime, result.problem, _assessed(result))


def _implement(runtime: Runtime, subject: str | None, outcome: Outcome, heartbeat: _Heartbeat) -> None:
    """指定对象时只推进它；否则同时推进至多 resources.concurrency.sessions 个互不相关的 Issue(见
    _issues_to_implement)，每个在自己的线程里、用自己的数据库连接(protocol/runtime.isolated)，各自的 worktree 与分支
    互不干扰。同批还有没做完的时按 _Refill 补人，一轮开始的总数到 resources.refillMax 为止。"""
    if subject is not None:
        issue = _issue_to_implement(runtime, subject)
        if issue is not None:
            _drive_issue(runtime, outcome, heartbeat, issue)
        return
    sessions = int(runtime.settings.get("resources.concurrency.sessions"))
    chosen = _issues_to_implement(runtime, sessions)
    if sessions <= 1:
        for issue in chosen:
            _drive_issue(runtime, outcome, heartbeat, issue)
        return
    refill = _Refill.of(runtime.settings, sessions)
    started = {issue.id for issue in chosen}
    with ThreadPoolExecutor(max_workers=sessions, thread_name_prefix="implement") as pool:
        running: dict[Future[None], issues.Issue] = {
            pool.submit(_drive_isolated, runtime, outcome, heartbeat, issue): issue for issue in chosen}
        freed: list[float] = []  # 空出来的位置各自从什么时候开始空着
        exhausted: tuple[frozenset[str], int, int] | None = None  # 上次没补满时的(在做的、空位数、该补数)
        while running:
            done, _ = wait(running, timeout=REFILL_POLL_S, return_when=FIRST_COMPLETED)
            for future in done:
                running.pop(future)
                future.result()
                freed.append(monotonic())
            if not running or _halted(runtime, heartbeat) is not None:
                continue
            room = min(refill.count(freed, monotonic()), refill.most - len(started))
            seen = (frozenset(issue.id for issue in running.values()), len(freed), room)
            if room <= 0 or seen == exhausted:  # 上次没补满之后什么都没变：不每隔几秒空查一遍
                continue
            busy = set().union(*(_touched_files(runtime, issue) for issue in running.values()))
            found = _issues_to_implement(runtime, room, skip=started, busy=busy)
            for issue in found:
                started.add(issue.id)
                freed.remove(min(freed))
                _emit(runtime, issue.id, "decision", "同批还有没做完的，补上空位")
                running[pool.submit(_drive_isolated, runtime, outcome, heartbeat, issue)] = issue
            if len(found) < room:
                exhausted = (frozenset(issue.id for issue in running.values()), len(freed),
                             min(refill.count(freed, monotonic()), refill.most - len(started)))


@dataclass(frozen=True)
class _Refill:
    """并行实施时什么时候补人(protocol/resources.md「并行实施时补人」)：同时空出 when_free 个位置就一起补上；不到这个数
    时，单个空位等过 after_s 也补(兜底)；一轮开始的总数到 most 为止。"""

    when_free: int
    after_s: float
    most: int

    @classmethod
    def of(cls, settings: Settings, sessions: int) -> _Refill:
        when_free, most = settings.get("resources.refillWhenFree"), settings.get("resources.refillMax")
        return cls(when_free=int(when_free) if when_free is not None else max(1, sessions // 2),
                   after_s=settings.duration("resources.refillAfter"),
                   most=int(most) if most is not None else REFILL_FACTOR * sessions)

    def count(self, freed: Sequence[float], now: float) -> int:
        """freed 为各空位开始空着的时刻：现在该补几个。"""
        if len(freed) >= self.when_free:
            return len(freed)
        return sum(now - at >= self.after_s for at in freed)


def _drive_isolated(runtime: Runtime, outcome: Outcome, heartbeat: _Heartbeat, issue: issues.Issue) -> None:
    with isolated(runtime) as local:
        _drive_issue(local, outcome, heartbeat, issue)


def _drive_issue(runtime: Runtime, outcome: Outcome, heartbeat: _Heartbeat, issue: issues.Issue) -> None:
    from tightrein.implement.implement import implement

    new = issue.status == TODO
    _drive(runtime, outcome, heartbeat, "implement", issue.id, new, lambda: implement(runtime, issue.id),
           still=lambda: stage_for(_status(runtime, issue.id)) == IMPLEMENT)


def _release(runtime: Runtime, subject: str | None, outcome: Outcome, heartbeat: _Heartbeat) -> None:
    """releasing 的 Issue 只交给合并队列(它决定先后与合并)；accepting 的与指定的对象单独推进。
    release() 一次推进到能走多远就走多远(等 CI、等部署、观察期、要人决定时停)，不重复调用。"""
    from tightrein.release.queue import queue
    from tightrein.release.release import release

    if subject is not None:
        if kind_of(subject) == "issue" and stage_for(_status(runtime, subject)) == RELEASE \
                and _skip_object(runtime, subject, new=False) is None:
            _object_step(runtime, outcome, "release", subject, lambda: release(runtime, subject))
        return
    results = _guarded(runtime, outcome, "release", None, lambda: queue(runtime), None) or []
    for result in results:
        outcome.steps.append(Step("release", result.subject, _step_status(result.status), result.summary))
    queued = {result.subject for result in results}  # 队列里刚合并转入验收的，本次已推进过，不再调用
    for item in issues.find(runtime.conn, status=ACCEPTING):
        if _halted(runtime, heartbeat) is not None:
            return
        if item.id not in queued and item.held_by is None and _skip_object(runtime, item.id, new=False) is None:
            _object_step(runtime, outcome, "release", item.id, partial(release, runtime, item.id))


def _retro(runtime: Runtime, subject: str | None, outcome: Outcome, heartbeat: _Heartbeat) -> None:
    from tightrein.retro.retro import retro

    _guarded(runtime, outcome, "retro", None, lambda: retro(runtime), lambda result: (StepStatus.PASSED, "复盘完成"))


# 对象


def _drive(runtime: Runtime, outcome: Outcome, heartbeat: _Heartbeat, stage: str, subject: str, new: bool,
           action: Callable[[], Any], *, still: Callable[[], bool]) -> None:
    """把一个对象一步一步往前推，直到暂停、停在关卡、失败、离开这个阶段或没有前进。"""
    previous: tuple[str | None, str | None] | None = None
    for _ in range(MAX_STEPS):
        if _halted(runtime, heartbeat) is not None:
            return
        skip = _skip_object(runtime, subject, new=new)
        if skip is not None:
            outcome.steps.append(Step(stage, subject, StepStatus.SKIPPED, skip))
            return
        new = False  # 只有第一步算「开始新的」
        result = _object_step(runtime, outcome, stage, subject, action)
        if result is None or _step_status(result.status) is not StepStatus.PASSED or result.next_point is None:
            return
        current = (result.point, result.next_point)
        if current == previous:  # 推了一步状态却没变：算一次失败，交给对象熔断
            _failed(runtime, subject, f"{stage}：推进后没有前进(停在 {result.point})")
            return
        previous = current
        if not still():
            return


def _object_step[T](runtime: Runtime, outcome: Outcome, stage: str, subject: str, action: Callable[[], T],
                    describe: Callable[[Any], str] | None = None) -> T | None:
    """对象的一步：成功即清零对象熔断计数，失败记一次；停在人工关卡不计数。"""
    breaker = runtime.agents.breaker

    def judge(value: Any) -> tuple[StepStatus, str]:
        return _step_status(value.status), (describe or _describe)(value)

    result = _guarded(runtime, outcome, stage, subject, action, judge)
    status = outcome.steps[-1].status
    if status is StepStatus.FAILED:
        _failed(runtime, subject, outcome.steps[-1].summary)
    elif status is StepStatus.PASSED:
        breaker.object_progressed(subject)
    return result


def _failed(runtime: Runtime, subject: str, reason: str) -> None:
    if not runtime.agents.breaker.object_failed(subject):
        return
    _emit(runtime, subject, "decision", f"对象熔断：连续失败，停止处理并交给人({reason})")
    if kind_of(subject) == "issue":
        from tightrein.assess.issue.transitions import InvalidTransition, IssueEvent, apply_event

        try:
            apply_event(runtime, subject, IssueEvent.TAKE, reason=BREAKER_HOLDER, actor=BREAKER_HOLDER, note=reason)
        except InvalidTransition as error:  # 已在别的状态(被关闭等)：只记下，不让熔断本身拖垮运行
            _emit(runtime, subject, "effect", f"对象熔断时接管失败：{error}")
    counters.reset(runtime.conn, BREAKER_PREFIX + subject, runtime.clock)


def _skip_object(runtime: Runtime, subject: str, *, new: bool) -> str | None:
    """开始一个对象(或它的下一步)前：熔断已打开的不碰；额度到了留余量的门槛时不开始新的。"""
    if object_tripped(runtime, subject):
        return "对象熔断已打开"
    if new and runtime.agents.quota.reserve_reached():
        return _reserve(runtime)
    return None


def _reserve(runtime: Runtime) -> str:
    return "额度到了留余量的门槛：" + "；".join(runtime.agents.quota.reserve_reasons())


def _trigger(runtime: Runtime) -> str | None:
    current = runs.get(runtime.conn, runtime.run)
    return current.trigger if current is not None else None


def _collected(result: Any) -> str:
    ran = [item.source for item in result.results]
    disabled = result.disabled
    return f"跑了 {len(ran)} 个来源，新问题 {len(result.new)}、回归 {len(result.regressed)}" + (
        f"；跳过 {len(result.skipped)} 个" if result.skipped else "") + (
        "；未启用 " + "、".join(f"{source}({reason})" for source, reason in disabled.items()) if disabled else "")


def _issue_to_implement(runtime: Runtime, subject: str | None) -> issues.Issue | None:
    """同时只处理一个 Issue：有进行中的先做它，没有才按严重度、编号开始一个待修的。"""
    if subject is not None:
        if kind_of(subject) != "issue":
            return None
        found = issues.get(runtime.conn, subject)
        return found if found is not None and stage_for(found.status) == IMPLEMENT and found.held_by is None else None
    for status in FOR_IMPLEMENT:
        candidates = [item for item in issues.find(runtime.conn, status=status) if item.held_by is None]
        if candidates:
            return min(candidates, key=lambda item: (SEVERITY_ORDER.get(item.severity or "", len(SEVERITY_ORDER)), item.id))
    return None


def _issues_to_implement(runtime: Runtime, limit: int, *, skip: AbstractSet[str] = frozenset(),
                         busy: AbstractSet[str] = frozenset()) -> list[issues.Issue]:
    """这一轮要推进的 Issue：进行中的先做，再按严重度、编号开始待修的，至多 limit 个；会改到的文件(_touched_files)与
    已选的(以及 busy，正在做的)有重叠的不同时做(各自的 PR 改同一个文件，后合并的要重新同步、可能冲突)，留到下一轮。
    skip 为这一轮已经推进过的(停在关卡的仍是进行中，不再重复挑)。还在等前置 Issue 合入的(assess/issue/blockers.py)、
    停在关卡等用户而还没有新决定的(implement.waiting_for_user)不挑：推了也马上停下，白占一个位置。"""
    from tightrein.implement.implement import waiting_for_user

    known = blockers.statuses(runtime.conn)
    candidates = [item for status in FOR_IMPLEMENT for item in sorted(
        (found for found in issues.find(runtime.conn, status=status) if found.held_by is None and found.id not in skip
         and not blockers.waiting(blockers.of(found), known)
         and not (found.status == IMPLEMENTING and waiting_for_user(runtime, found.id))),
        key=lambda item: (SEVERITY_ORDER.get(item.severity or "", len(SEVERITY_ORDER)), item.id))]
    chosen: list[issues.Issue] = []
    taken: set[str] = set(busy)
    for item in candidates:
        files = _touched_files(runtime, item)
        if taken & files:
            continue
        chosen.append(item)
        taken |= files
        if len(chosen) >= max(limit, 1):
            break
    return chosen


def _touched_files(runtime: Runtime, issue: issues.Issue) -> set[str]:
    """这个 Issue 会改到的文件：根因所在的文件、评估预估要改的(extra.plannedFiles)、方案的文件清单与各步的文件、修复
    worktree 中已经改动的(未提交的与未跟踪的)。只认列出来的，不按源文件去推测测试文件。只看根因文件不够：根因不同的
    几个 Issue 曾同时改了同一个存储模块与它的测试，发布时合并主干后有两个要退回实施重新审查。"""
    found = {str(cause["file"]) for cause in issue.extra.get("rootCauses") or []
             if isinstance(cause, dict) and cause.get("file")}
    found |= {str(path) for path in issue.extra.get(PLANNED_FILES) or [] if path}
    design = _facts(runtime, issue.id, DESIGN_POINT)
    found |= {str(item["path"]) for item in design.get("files") or [] if isinstance(item, dict) and item.get("path")}
    found |= {str(item["file"]) for item in design.get("steps") or [] if isinstance(item, dict) and item.get("file")}
    worktree = _facts(runtime, issue.id, PREPARE_POINT).get("worktree")
    if worktree and Path(str(worktree)).is_dir():
        found |= set(runtime.git.at(Path(str(worktree))).status().changed_paths)
    return found


def _facts(runtime: Runtime, issue: str, point: str) -> dict[str, Any]:
    """Issue 某个实施小步骤最近一次交接的事实；还没做到时为空。"""
    path = runtime.workspace.step_file(issue, FileName(point, "handoff", "json"))
    return dict(read(path).facts) if path.is_file() else {}


def _status(runtime: Runtime, issue: str) -> str | None:
    found = issues.get(runtime.conn, issue)
    return None if found is None or found.held_by is not None else found.status


# 错误边界与记录


def _guarded[T](runtime: Runtime, outcome: Outcome, stage: str, subject: str | None, action: Callable[[], T],
             judge: Callable[[T], tuple[StepStatus, str]] | None) -> T | None:
    """一步的错误边界：抛出的异常(中断除外)只记为这一步失败，后面的步骤与其他对象照常。"""
    try:
        result = action()
    except Exception as error:  # noqa: BLE001 错误边界：一步失败不拖垮整次运行
        summary = f"{type(error).__name__}: {error}"
        outcome.steps.append(Step(stage, subject, StepStatus.FAILED, summary))
        _emit(runtime, subject, "effect", f"{stage} 失败：{summary}")
        return None
    if judge is not None:
        status, summary = judge(result)
        outcome.steps.append(Step(stage, subject, status, summary))
    return result


def _step_status(status: Any) -> StepStatus:
    """一步的结论 → 调度里这一步的结果：按流程退回(returned)不算失败，对象照常算有进展。"""
    return {Status.PASSED: StepStatus.PASSED, Status.FAILED: StepStatus.FAILED,
            Status.PENDING: StepStatus.PENDING, Status.RETURNED: StepStatus.PASSED}[Status(status)]


def _assessed(result: Any) -> str:
    """评估结果的一句话：判定 → 去向(写成了 Issue 时带编号)。"""
    text = f"{result.verdict} → {result.disposition}" + (f"，Issue {result.issue}" if result.issue else "")
    reason = getattr(result, "reason", None)
    return f"{text}：{reason}" if reason else text


def _describe(result: Any) -> str:
    summary = getattr(result, "summary", None)
    return summary if isinstance(summary, str) and summary else str(Status(result.status).value)


def _summary(outcome: Outcome) -> dict[str, Any]:
    return {"halted": outcome.halted,
            "steps": [{"stage": step.stage, "subject": step.subject, "status": step.status.value,
                       "summary": step.summary} for step in outcome.steps]}


def _emit(runtime: Runtime, subject: str | None, kind: str, summary: str) -> None:
    runtime.events.emit(run=runtime.run, subject=subject, point=POINT, kind=kind, summary=summary)


# 开始前的判断


def _refusal(runtime: Runtime) -> str | None:
    control = recovery.control(runtime.workspace)
    if control is not None:
        return f"处于{'暂停' if control.mode is recovery.Mode.PAUSED else '急停'}(自 {control.since})：tightrein resume 恢复"
    halted = runtime.agents.quota.halted_until()
    if halted is not None:
        return f"订阅额度已用完，{halted.isoformat()} 重置后再跑"
    return None


def _check_subject(runtime: Runtime, subject: str) -> None:
    """不认识的编号、不存在的对象直接报错(用法错误)，不开始运行。"""
    kind = kind_of(subject)
    found = issues.get(runtime.conn, subject) if kind == "issue" else problems.get(runtime.conn, subject) \
        if kind == "problem" else None
    if found is None:
        raise LookupError(f"没有 {subject}")


def _not_now(runtime: Runtime, trigger: str) -> str | None:
    if not state.get(runtime.conn, READY_KEY):
        return "项目还没有就绪：tightrein project check 通过后执行 tightrein project ready"
    if trigger == "schedule" and not in_window(runtime.settings, runtime.clock.now()):
        window = runtime.settings.get("schedule.window")
        return f"不在能跑的时段({window['from']}–{window['to']})"
    return None


def _note_wake(runtime: Runtime) -> None:
    """记下这次定时醒来，并算出距上次醒来漏掉了几次(status 的健康项显示)。"""
    now = runtime.clock.now()
    last = state.get(runtime.conn, WAKE_KEY)
    tick = runtime.settings.duration("schedule.tick")
    missed = max(0, int((now - parse_iso(last)).total_seconds() // tick) - 1) if last and tick > 0 else 0
    state.put(runtime.conn, WAKE_KEY, format_iso(now), runtime.clock)
    state.put(runtime.conn, MISSED_KEY, missed, runtime.clock)


def _halted(runtime: Runtime, heartbeat: _Heartbeat) -> str | None:
    if heartbeat.lost is not None:
        return heartbeat.lost
    control = recovery.control(runtime.workspace)
    if control is None:
        return None
    return "已暂停" if control.mode is recovery.Mode.PAUSED else "已急停"


def _stages(runtime: Runtime, trigger: str, stage: str | None, subject: str | None) -> list[str]:
    """要跑的阶段：指定了就只跑它；定时与事件触发只自动推进到 schedule.advanceTo，复盘总是最后跑一次。

    一轮之内按 RUN_ORDER 先实施、发布已放行的 Issue，再采集、评估新问题：改动先出来，不被新问题的评估挡住；
    这一轮评估新建的 Issue 留到下一轮实施。"""
    if stage is not None:
        return [stage]
    if subject is not None:
        return ["assess"] if kind_of(subject) == "problem" else ["implement", "release"]
    allowed = list(STAGES)
    if trigger in AUTOMATIC:
        allowed = allowed[:allowed.index(str(runtime.settings.get("schedule.advanceTo"))) + 1] + ["retro"]
    return [name for name in RUN_ORDER if name in allowed]


def _run_stage(stages: list[str]) -> str:
    return stages[0] if len(stages) == 1 else "run"


def _plan(runtime: Runtime, stages: list[str], subject: str | None, trigger: str) -> list[Planned]:
    from tightrein.assess import select
    from tightrein.collect import collect

    planned: list[Planned] = []
    for name in stages:
        if name == "collect":
            selected, _ = collect.select(runtime, only=collect.sources(runtime) if trigger == MANUAL else None)
            planned += [Planned(name, item.source, f"错过 {item.missed} 次" if item.missed else "到点")
                        for item in selected]
        elif name == "assess":
            if subject is not None:
                found = [subject] if kind_of(subject) == "problem" else []
            else:
                found = [item.id for item in select.pending(runtime.conn)[:int(runtime.settings.section("assess")[
                    "perRun"])]]
            planned += [Planned(name, problem, "待评估") for problem in found]
        elif name == "implement":
            found_issue = _issue_to_implement(runtime, subject) if subject is not None else None
            chosen = ([found_issue] if found_issue is not None else []) if subject is not None else \
                _issues_to_implement(runtime, int(runtime.settings.get("resources.concurrency.sessions")))
            planned += [Planned(name, issue.id, issue.status) for issue in chosen]
        elif name == "release":
            planned += [Planned(name, item.id, item.status) for status in FOR_RELEASE
                        for item in issues.find(runtime.conn, status=status)
                        if item.held_by is None and (subject is None or item.id == subject)]
        else:
            planned.append(Planned(name, None, "每次运行结束都执行"))
    return planned


def _skipped_busy(runtime: Runtime, trigger: str, stages: list[str], reason: str) -> Outcome:
    """运行锁被占：不等待；定时与事件触发记一条「跳过」，供 status 与复盘看到。"""
    if trigger in AUTOMATIC:
        now = runtime.clock.now()
        runs.start(runtime.conn, runs.Run(id=runtime.run, stage=_run_stage(stages), trigger=trigger, status="running",
                                          started_at=now))
        runs.finish(runtime.conn, runtime.run, RunStatus.SKIPPED.value, runtime.clock, summary={"reason": reason})
    return Outcome(runtime.run, RunStatus.REFUSED, f"上一次运行还没结束：{reason}")


class _Heartbeat:
    """后台续心跳：运行锁与 runs 表各一份。SQLite 连接不能跨线程，线程里另开一个。"""

    def __init__(self, lock: FileLock, runtime: Runtime) -> None:
        self.lock = lock
        self.database = runtime.workspace.database
        self.run = runtime.run
        self.clock = runtime.clock
        self.interval_s = runtime.settings.duration("limits.lock.heartbeat")
        self.lost: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="tightrein-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _loop(self) -> None:
        conn = connect(self.database)
        try:
            while not self._stop.wait(self.interval_s):
                try:
                    self.lock.beat()
                except Lost as error:
                    self.lost = f"运行锁已被接管：{error}"
                    return
                runs.heartbeat(conn, self.run, self.clock)
        finally:
            conn.close()
