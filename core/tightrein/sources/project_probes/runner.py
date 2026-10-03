"""运行一个项目探针：经标准输入交给它输入 JSON，从标准输出读回输出 JSON 并按 schema 校验，再构造信号。

- 命令相对工作区执行，{python} 替换为核心的解释器(便于 import tightrein.sources.project_probes.helpers)；环境变量经
  guards.credentials.build_env 过滤，另加 TIGHTREIN_WORKSPACE、TIGHTREIN_PROBE_NAME 与登记的钥匙串条目名
  (TIGHTREIN_PROBE_KEYCHAIN，逗号分隔，helpers.secret 只允许读这些条目)；
- 退出码非 0、超时、输出不是 JSON 或不合 schema 时本次作废：不产出信号、不保存状态，原因写进说明；标准错误经脱敏
  写进原始输出目录的 <探针名>.stderr.log；
- 信号：source 为 behavior，check 为探针名，message 为 symptom，occurred_at 取 occurredAt(省略时为本次运行的时间)，
  context 带 sourceName(探针名)、probeFingerprint、evidence、severityHint 与 details(输出中的 context)。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.config.layers import core_value
from tightrein.contracts import validate
from tightrein.domain.clock import format_iso, parse_iso
from tightrein.domain.enums import Source
from tightrein.domain.signal import Signal
from tightrein.extensions.invoke import ProcessRequest, ProcessRunner
from tightrein.extensions.points import PYTHON_PLACEHOLDER
from tightrein.guards.credentials import build_env
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.signals import SignalFactory
from tightrein.sources.platform_errors.logs import ReleaseAt
from tightrein.sources.project_probes.registry import Registration

INPUT_SCHEMA = "data/project-probe-input.schema.json"
OUTPUT_SCHEMA = "data/project-probe-output.schema.json"
ENV_WORKSPACE = "TIGHTREIN_WORKSPACE"
ENV_NAME = "TIGHTREIN_PROBE_NAME"
ENV_KEYCHAIN = "TIGHTREIN_PROBE_KEYCHAIN"
STDERR_SUFFIX = ".stderr.log"


@dataclass(frozen=True)
class ProbeRun:
    name: str
    output: Mapping[str, Any] | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.output is not None


def build_input(item: Registration, workspace: Path, last_run_at: datetime | None, state: Mapping[str, Any] | None,
                since: datetime, until: datetime, environment: str, base_url: str | None) -> dict[str, Any]:
    document = {"name": item.name, "lastRunAt": None if last_run_at is None else format_iso(last_run_at),
                "state": None if state is None else dict(state),
                "window": {"since": format_iso(since), "until": format_iso(until)},
                "workspace": str(workspace.absolute()), "environment": environment, "baseUrl": base_url}
    validate.check(INPUT_SCHEMA, document)
    return document


def argv(item: Registration, python: str = sys.executable) -> tuple[str, ...]:
    return tuple(python if part == PYTHON_PLACEHOLDER else part for part in item.command)


def execute(item: Registration, document: Mapping[str, Any], *, workspace: Path, runner: ProcessRunner,
            environ: Mapping[str, str], redactor: ProbeRedactor, raw_dir: Path | None) -> ProbeRun:
    env = {**build_env(environ).env, ENV_WORKSPACE: str(workspace.absolute()), ENV_NAME: item.name,
           ENV_KEYCHAIN: ",".join(item.keychain)}
    timeout = item.timeout_seconds or float(core_value("runtime.sources.probeTimeoutSeconds"))
    outcome = runner(ProcessRequest(argv(item), workspace, env, json.dumps(document, ensure_ascii=False).encode(),
                                    timeout))
    stderr = redactor.text(outcome.stderr.decode("utf-8", errors="replace"))
    if raw_dir is not None and stderr.strip():
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{item.name}{STDERR_SUFFIX}").write_text(stderr, encoding="utf-8")
    if outcome.start_error is not None:
        return ProbeRun(item.name, None, f"无法启动：{outcome.start_error}")
    if outcome.timed_out:
        return ProbeRun(item.name, None, f"超过 {timeout:g} 秒未结束，已终止")
    if outcome.exit_code != 0:
        tail = " / ".join(stderr.strip().splitlines()[-3:]) or "没有错误输出"
        return ProbeRun(item.name, None, f"退出码 {outcome.exit_code}：{tail}")
    try:
        output = json.loads(outcome.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ProbeRun(item.name, None, "标准输出不是 JSON")
    errors = validate.validate(OUTPUT_SCHEMA, output)
    if errors:
        return ProbeRun(item.name, None, "输出不符合探针契约：" + "；".join(str(error) for error in errors[:5]))
    return ProbeRun(item.name, output)


def to_signals(name: str, output: Mapping[str, Any], factory: SignalFactory, release_at: ReleaseAt,
               now: datetime) -> list[Signal]:
    signals = []
    for item in output["signals"]:
        occurred = parse_iso(item["occurredAt"]) if item.get("occurredAt") else now
        signals.append(factory.create(
            source=Source.BEHAVIOR, check=name, location=item["location"], message=item["symptom"],
            occurred_at=occurred, release=release_at(occurred),
            context={"sourceName": name, "probeFingerprint": item["fingerprint"], "evidence": list(item["evidence"]),
                     "severityHint": item["severityHint"], "details": dict(item.get("context") or {})}))
    return signals
