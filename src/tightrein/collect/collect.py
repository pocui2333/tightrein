"""采集一轮(44c `run(runtime) -> CollectOutcome`)：选出到点的来源 → 并行运行 → 去重 → 交出新发现与回归的问题。

- 来源：接入清单中启用(或自定义)的 collect.* 模块，按 `tightrein.<来源>.source.collect(runtime)` 动态调用，
  新增来源只加模块与清单，不改这里；
- 到点：schedule.every 的间隔从 state 表记的上次时间算起；从未跑过的立即跑；错过多个时刻只补跑一次并记下错过次数；
  依赖代码或部署的来源(on_commit、on_deploy)只在仓库 HEAD 或最近一次成功部署变了时跑；
  失败的来源不记「已跑过」，下次醒来重新触发；上次时间与标记随来源的读取位置在去重的同一个事务里保存；
- 并行：互不依赖的来源同时跑，同时数受 resources.concurrency.collectSources 限制；每个来源一个独立的数据库连接
  (SQLite 连接不能跨线程)；每个来源有超时(controls.<来源>.sourceTimeout，与模型调用的 timeout 分开)，
  超时的记为失败、本轮不再等它，不拖住整轮；
  一个来源出错只记为该来源失败，其余照常；
- 开始前先刷新部署记录(接入清单启用了 release.deploy 时)：信号的 release 与 on_deploy 的判断都靠它；读不到时沿用已记的；
- 每个来源与去重各写一份交接文档(运行目录)：来源的交接带上本次的全部信号(内容存文件，数据库只存出现)；
- 接入清单中没有启用的来源连同原因列进结果(disabled)，写进运行摘要，不是静默跳过。
"""

from __future__ import annotations

import importlib
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from tightrein.collect.common.signals import deployments, latest_release
from tightrein.collect.common.source import SourceResult, SourceStatus, failed, guarded
from tightrein.collect.dedup.dedup import DedupOutcome, dedup, recover
from tightrein.onboard.setup import MODULES
from tightrein.protocol import handoff
from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.naming import FileName, format_duration, format_iso, parse_duration, parse_iso
from tightrein.protocol.runtime import isolated
from tightrein.release import deploy
from tightrein.store.tables import state

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

STAGE = "collect"
SLOT = "collectSources"
SCHEDULE_KEY = "collect.schedule:{source}"  # state 表：{"at": 上次运行时间, "marker": 上次的 commit}
SOURCE_MODULE = "tightrein.{source}.source"
ON_COMMIT, ON_DEPLOY = "on_commit", "on_deploy"
POLL_S = 0.05  # 等待来源结束时检查超时的间隔

SourceFunction = Callable[["Runtime"], SourceResult]
Loader = Callable[[str], SourceFunction]


@dataclass(frozen=True)
class Due:
    source: str
    marker: str | None  # 事件触发的来源本次对应的 commit；按间隔的为 None
    missed: int = 0  # 错过的时刻数(只补跑一次)


@dataclass
class CollectOutcome:
    new: list[str]
    regressed: list[str]
    results: list[SourceResult]
    skipped: dict[str, str]  # 没到点、没有新提交或新部署的来源 → 原因
    dedup: DedupOutcome | None  # 本轮没有来源运行时为 None
    notes: list[str] = field(default_factory=list)
    disabled: dict[str, str] = field(default_factory=dict)  # 接入清单中没有启用的来源 → 原因


def run(runtime: Runtime, *, only: Sequence[str] | None = None, loader: Loader | None = None,
        monotonic: Callable[[], float] = time.monotonic) -> CollectOutcome:
    """only 给出时(手动触发)只跑这几个来源，不看是否到点。"""
    recover(runtime)
    notes = _refresh_deployments(runtime)
    selected, skipped = select(runtime, only=only)
    disabled = disabled_sources(runtime)
    if not selected:
        return CollectOutcome([], [], [], skipped, None, notes, disabled)
    notes += [f"{item.source} 错过 {item.missed} 次，只补跑一次" for item in selected if item.missed]
    results = run_sources(runtime, [item.source for item in selected], loader or load_source, monotonic)
    marked = [_mark(runtime, result, selected) for result in results]
    for result in marked:
        _write_handoff(runtime, result)
    outcome = dedup(runtime, marked)
    return CollectOutcome(outcome.new, outcome.regressed, marked, skipped, outcome, notes + outcome.notes, disabled)


def sources(runtime: Runtime) -> list[str]:
    """接入清单中启用(或自定义)的采集来源，按 MODULES 的顺序。"""
    return [key for key in MODULES if key.startswith(f"{STAGE}.") and runtime.setup.enabled(key)]


def disabled_sources(runtime: Runtime) -> dict[str, str]:
    """接入清单中没有启用的采集来源 → 原因(清单里写的不启用原因)。"""
    return {key: runtime.setup.module(key).reason or "接入清单中没有写原因" for key in MODULES
            if key.startswith(f"{STAGE}.") and not runtime.setup.enabled(key)}


def select(runtime: Runtime, *, only: Sequence[str] | None = None) -> tuple[list[Due], dict[str, str]]:
    """(到点要跑的来源, 不跑的来源 → 原因)。"""
    every = runtime.settings.get("schedule.every")
    now = runtime.clock.now()
    markers = _Markers(runtime)
    selected: list[Due] = []
    skipped: dict[str, str] = {}
    for source in sources(runtime):
        if only is not None:
            if source in only:
                selected.append(Due(source, markers.of(every[source])))
            continue
        last = state.get(runtime.conn, SCHEDULE_KEY.format(source=source)) or {}
        found = _due(source, every[source], last, now, markers)
        if isinstance(found, Due):
            selected.append(found)
        else:
            skipped[source] = found
    return selected, skipped


def run_sources(runtime: Runtime, names: Sequence[str], loader: Loader,
                monotonic: Callable[[], float] = time.monotonic) -> list[SourceResult]:
    """并行运行各来源，按 names 的顺序返回结果；超时的记为失败，本轮不再等它。

    用守护线程而不是线程池：线程池的线程在进程退出时会被等待，一个卡住的来源会拖住整个进程。
    """
    timeouts = {name: parse_duration(runtime.settings.control(name, "sourceTimeout")) for name in names}
    started: dict[str, float] = {}  # 各线程只写自己的键；拿到并发名额后才开始计时
    finished: queue.Queue[tuple[str, SourceResult]] = queue.Queue()
    for name in names:
        threading.Thread(target=lambda name=name: finished.put((name, _run_one(runtime, name, loader, started,
                                                                               monotonic))),
                         name=f"{STAGE}:{name}", daemon=True).start()
    results: dict[str, SourceResult] = {}
    while len(results) < len(names):
        try:
            name, result = finished.get(timeout=POLL_S)
            results.setdefault(name, result)
        except queue.Empty:
            pass
        now = monotonic()
        for name in names:
            if name not in results and name in started and now - started[name] >= timeouts[name]:
                results[name] = failed(name, f"超过 {format_duration(timeouts[name])} 未结束，本轮不再等它")
    return [results[name] for name in names]


def load_source(source: str) -> SourceFunction:
    module = importlib.import_module(SOURCE_MODULE.format(source=source))
    return module.collect  # type: ignore[no-any-return]


class _Markers:
    """事件触发的标记：仓库 HEAD 与最近一次成功部署的 commit，一轮只取一次。"""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.found: dict[str, str | None] = {}

    def of(self, every: str) -> str | None:
        if every not in (ON_COMMIT, ON_DEPLOY):
            return None
        if every not in self.found:
            self.found[every] = (self.runtime.git.head().commit if every == ON_COMMIT
                                 else latest_release(deployments(self.runtime.conn)))
        return self.found[every]


def _due(source: str, every: str, last: dict[str, Any], now: datetime, markers: _Markers) -> Due | str:
    if every in (ON_COMMIT, ON_DEPLOY):
        marker = markers.of(every)
        what = "提交" if every == ON_COMMIT else "部署"
        if marker is None:
            return f"还没有{what}记录"
        if last.get("marker") == marker:
            return f"没有新{what}(上次跑在 {marker[:12]})"
        return Due(source, marker)
    interval = timedelta(seconds=parse_duration(every))
    if not last.get("at"):
        return Due(source, None)
    elapsed = now - parse_iso(last["at"])
    if elapsed < interval:
        return f"未到点：下次在 {format_iso(parse_iso(last['at']) + interval)}"
    return Due(source, None, missed=int(elapsed / interval) - 1)


def _run_one(runtime: Runtime, name: str, loader: Loader, started: dict[str, float],
             monotonic: Callable[[], float]) -> SourceResult:
    with runtime.slots.hold(SLOT):
        started[name] = monotonic()
        try:
            return guarded(name, lambda: _isolated(runtime, name, loader), monotonic=monotonic)
        except Exception as error:  # noqa: BLE001 一个来源的程序错误不拖垮整轮：记为该来源失败，原因写清类型
            runtime.events.emit(run=runtime.run, subject=runtime.run, point=name, kind="effect",
                                summary=f"{name} 出错：{type(error).__name__}")
            return failed(name, f"程序错误：{type(error).__name__}: {error}")


def _isolated(runtime: Runtime, name: str, loader: Loader) -> SourceResult:
    """来源在 guarded 起的线程里跑：连接等在这个线程里另开(protocol/runtime.isolated)。"""
    with isolated(runtime) as local:
        return loader(name)(local)


def _mark(runtime: Runtime, result: SourceResult, selected: Sequence[Due]) -> SourceResult:
    """跑过(没失败)的来源记下时间与标记，随读取位置在去重的事务里保存。"""
    if result.status is SourceStatus.FAILED:
        return result
    marker = next(item.marker for item in selected if item.source == result.source)
    value = {"at": format_iso(runtime.clock.now()), "marker": marker}
    return replace(result, state={**result.state, SCHEDULE_KEY.format(source=result.source): value})


def _refresh_deployments(runtime: Runtime) -> list[str]:
    """读一次并记进 state 表(同一次运行内发布阶段复用，不再读)；没启用部署来源时什么都不做。"""
    try:
        deploy.deployments(runtime)
    except deploy.DeployError as error:
        return [f"部署记录没有刷新，沿用已记的：{error}"]
    return []


def _write_handoff(runtime: Runtime, result: SourceResult) -> None:
    status = Status.FAILED if result.status is SourceStatus.FAILED else Status.PASSED
    summary = (f"{result.status.value}：读到 {result.read} 条，信号 {len(result.signals)} 条"
               + (f"；{result.reason}" if result.reason else ""))
    facts = {
        "status": result.status.value, "read": result.read, "window": list(result.window) if result.window else None,
        "reason": result.reason, "coverage": list(result.coverage), "notes": list(result.notes),
        "signals": [signal.to_json() for signal in result.signals],
    }
    path = runtime.workspace.step_file(runtime.run, FileName(result.source, "handoff", "json"))
    handoff.write(path, Handoff(point=result.source, subject=runtime.run, run=runtime.run, status=status,
                                summary=summary, facts=facts, metrics=result.metrics,
                                created_at=format_iso(runtime.clock.now())))

