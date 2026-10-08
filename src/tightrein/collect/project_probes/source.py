"""项目探针的调用：按 controls."collect.project_probes".probes 的登记运行到期的探针脚本，校验输出，转成信号。

- 每个探针的窗口是上次成功运行的时间到现在(第一次回看 lookback)，上次输出的 state 原样交回；按 every 判断到期；
- 探针经 common/scripts 运行：环境变量只给白名单，另加工作区、探针名与登记过的凭据(secrets.json 中的条目)；
- 退出码非 0、超时、输出不是 JSON 或不符合 output.schema.json 时本次作废：不产出信号、不保存状态，下次到期重试；
  标准错误脱敏后写进原始输出；
- 到期的探针并行运行，各自成败：都作废为 failed，部分作废为 partial；
- 指纹为「探针名:探针给的 fingerprint」(group_key)；成功运行的探针名记进覆盖范围；
- trial 只校验并返回会产出的信号，不保存任何东西(接入时试跑用)。
契约见本目录 README.md 的「契约」，写法见本目录的方法文档。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.collect.common.signals import ReleaseAt, Signal, SignalFactory, factory_for, releases
from tightrein.collect.common.source import (
    SourceError,
    SourceInvalid,
    SourceMisconfigured,
    SourceResult,
    SourceStatus,
    describe,
    each,
    failed,
    skipped,
)
from tightrein.protocol import scripts
from tightrein.protocol.handoff import Metrics, load_schema, schema_errors
from tightrein.protocol.naming import format_iso, parse_duration, parse_iso
from tightrein.protocol.raw import RawDir, raw_dir
from tightrein.store.tables import state as state_table

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

SOURCE = "collect.project_probes"
CHECK_TYPE = "probe"
OUTPUT_SCHEMA = Path(__file__).with_name("output.schema.json")
LAST_RUN = "lastRunAt"
STATE = "state"
TARGET_SITE = "target"  # sites.json 中被测系统的 baseUrl 与 environment
DISABLED = '没有登记项目探针：controls."collect.project_probes".probes 为空'
NONE_DUE = "没有到期的项目探针"


@dataclass(frozen=True)
class Registration:
    name: str
    command: tuple[str, ...]
    every: timedelta
    secrets: tuple[str, ...]
    timeout_s: float

    @property
    def state_key(self) -> str:
        return f"{SOURCE}:{self.name}"


@dataclass(frozen=True)
class Previous:
    last_run_at: datetime | None
    state: dict[str, Any] | None


@dataclass
class ProbeRun:
    name: str
    signals: list[Signal]
    state: dict[str, Any] | None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Trial:
    name: str
    signals: list[Signal]
    notes: list[str]
    error: str | None  # 作废的原因；None 为输出符合契约


def collect(runtime: Runtime, names: Sequence[str] = ()) -> SourceResult:
    """names 给出时只运行这几个(不看间隔)；否则运行到期的。"""
    module = runtime.setup.module(SOURCE)
    if not runtime.setup.enabled(SOURCE):
        return skipped(SOURCE, f"未启用：{module.reason or '接入清单中为 disabled'}")
    items = registered(runtime.settings.section(SOURCE))
    if not items:
        return skipped(SOURCE, DISABLED)
    now = runtime.clock.now()
    previous = {item.name: _previous(runtime, item) for item in items}
    chosen = [find(items, name) for name in names] if names else due(items, previous, now)
    if not chosen:
        return skipped(SOURCE, NONE_DUE)
    factory, release_at = factory_for(runtime, SOURCE), releases(runtime.conn)
    found = each({item.name: _job(runtime, item, previous[item.name], now, factory, release_at) for item in chosen},
                 workers=int(runtime.settings.get("resources.concurrency.collectSources")))
    runs = [item for item in found.values() if isinstance(item, ProbeRun)]
    errors = [f"项目探针 {name} 本次作废：{describe(item)}" for name, item in found.items()
              if isinstance(item, SourceError)]
    notes = errors + [f"{run.name}：{note}" for run in runs for note in run.notes]
    if not runs:
        return failed(SOURCE, "；".join(errors), notes=notes)
    states: dict[str, Any] = {find(items, run.name).state_key: {LAST_RUN: format_iso(now), STATE: run.state}
                              for run in runs}
    signals = [signal for run in runs for signal in run.signals]
    produced = {"probes": len(chosen), "succeeded": len(runs), "signals": len(signals)}
    return SourceResult(SOURCE, SourceStatus.PARTIAL if errors else SourceStatus.DONE, signals, len(runs), None,
                        "；".join(errors) or None, states, Metrics(produced=produced),
                        coverage=[run.name for run in runs], notes=notes)


def trial(runtime: Runtime, name: str) -> Trial:
    item = find(registered(runtime.settings.section(SOURCE)), name)
    try:
        run = _job(runtime, item, _previous(runtime, item), runtime.clock.now(), factory_for(runtime, SOURCE),
                   releases(runtime.conn))()
    except SourceError as error:
        return Trial(name, [], [], describe(error))
    return Trial(name, run.signals, run.notes, None)


def registered(section: Mapping[str, Any]) -> list[Registration]:
    default_timeout = parse_duration(section["probeTimeout"])
    found = []
    for index, item in enumerate(section.get("probes") or []):
        try:
            timeout = parse_duration(item["timeout"]) if item.get("timeout") else default_timeout
            found.append(Registration(item["name"], tuple(item["command"]),
                                      timedelta(seconds=parse_duration(item["every"])),
                                      tuple(item.get("secrets") or ()), timeout))
        except (KeyError, TypeError, ValueError) as error:
            message = f'controls."{SOURCE}".probes[{index}] 不合格：要有 name、command、every'
            raise SourceMisconfigured(message) from error
    return found


def find(items: Sequence[Registration], name: str) -> Registration:
    found = next((item for item in items if item.name == name), None)
    if found is None:
        raise SourceMisconfigured(f'controls."{SOURCE}".probes 中没有登记探针 {name}')
    return found


def due(items: Sequence[Registration], previous: Mapping[str, Previous], now: datetime) -> list[Registration]:
    found = []
    for item in items:
        last = previous[item.name].last_run_at
        if last is None or now - last >= item.every:
            found.append(item)
    return found


def to_signals(item: Registration, output: Mapping[str, Any], factory: SignalFactory, release_at: ReleaseAt,
               now: datetime, environment: str | None) -> list[Signal]:
    signals = []
    for found in output["signals"]:
        occurred = parse_iso(found["occurredAt"]) if found.get("occurredAt") else now
        signals.append(factory.create(
            check_type=CHECK_TYPE, location=found["location"], message=found["symptom"], occurred_at=occurred,
            commit=release_at(occurred), environment=environment, severity_hint=found["severityHint"],
            group_key=f"{item.name}:{found['fingerprint']}",
            evidence={"sourceName": item.name, "probeFingerprint": found["fingerprint"],
                      "facts": list(found["evidence"]), "details": dict(found.get("context") or {})}))
    return signals


def _previous(runtime: Runtime, item: Registration) -> Previous:
    saved = state_table.get(runtime.conn, item.state_key)
    if not isinstance(saved, Mapping) or not saved.get(LAST_RUN):
        return Previous(None, None)
    return Previous(parse_iso(saved[LAST_RUN]), saved.get(STATE))


def _job(runtime: Runtime, item: Registration, previous: Previous, now: datetime, factory: SignalFactory,
         release_at: ReleaseAt) -> Callable[[], ProbeRun]:
    target = runtime.settings.sites.get(TARGET_SITE) or {}
    lookback = parse_duration(runtime.settings.section(SOURCE)["lookback"])

    def job() -> ProbeRun:
        since = previous.last_run_at or now - timedelta(seconds=lookback)
        last_run = None if previous.last_run_at is None else format_iso(previous.last_run_at)
        document = {"name": item.name, "lastRunAt": last_run, "state": previous.state,
                    "window": {"since": format_iso(since), "until": format_iso(now)},
                    "workspace": str(runtime.workspace.root.absolute()), "environment": target.get("environment"),
                    "baseUrl": target.get("baseUrl")}
        stdout = scripts.run(name=item.name, command=item.command, document=document,
                             workspace=runtime.workspace.root, runner=runtime.runner, environ=runtime.environ,
                             secrets=runtime.secrets,
                             secret_names=item.secrets, redactor=runtime.redactor,
                             raw=RawDir(raw_dir(runtime.workspace, runtime.run, SOURCE)), timeout_s=item.timeout_s)
        output = _output(stdout)
        return ProbeRun(item.name, to_signals(item, output, factory, release_at, now, target.get("environment")),
                        output.get("state"), list(output.get("notes") or []))

    return job


def _output(stdout: str) -> dict[str, Any]:
    try:
        output = json.loads(stdout)
    except ValueError as error:
        raise SourceInvalid("标准输出不是 JSON") from error
    errors = schema_errors(output, load_schema(OUTPUT_SCHEMA))
    if errors:
        raise SourceInvalid("输出不符合探针契约：" + "；".join(errors[:5]))
    return output
