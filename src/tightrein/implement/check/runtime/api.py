"""只对改动涉及的接口：5xx 模糊测试的浅跑(Schemathesis，复用 collect/api_fuzz/schemathesis)。

- 受影响的接口 = 方案交接的 affectedEndpoints(`方法 路由`)，加上端点清单中处理方法所在文件属于改动文件的项；
  端点清单是项目的一个 JSON 文件(`api.endpoints`，相对 worktree：`[{method, route, sourceFile}]`)，没有时只用方案的；
- 接口描述(`api.spec`)：以 `/` 开头时从本机启动的接口服务取，否则为 worktree 中的文件；没有时记未验证；
- 与基准比对：出错的接口按采集的去重规则算指纹(与 collect/api_fuzz 产出的信号同一算法)，与改动前就已存在、未解决、
  不属于本 Issue 的问题的指纹与别名比对，再按规范化后的位置比对；已存在的只在说明中列出，只有新出现的计为失败。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.collect.api_fuzz.mapping import CHECK_TYPE
from tightrein.collect.api_fuzz.schemathesis import config_writer, invoke, report_parser
from tightrein.collect.common.signals import Signal
from tightrein.collect.dedup import group
from tightrein.collect.dedup.normalize import location as normalize_location
from tightrein.collect.dedup.normalize import message as normalize_message
from tightrein.implement.check.runtime.verdict import Item, Result, unverified
from tightrein.protocol.http import HttpRequest, Transport
from tightrein.protocol.naming import parse_duration
from tightrein.protocol.process import ProcessRunner
from tightrein.settings.load import Settings
from tightrein.store.tables import problems

CATEGORY = "api"
SOURCE = "collect.api_fuzz"
ALIASES = "aliases"  # problems.extra 中并入的其他指纹
CLOSED = frozenset({"resolved", "closed", "muted"})
SPEC_FILE = "openapi.json"
CONFIG_FILE = "schemathesis.toml"
SECRET_TOKEN = "api.token"


@dataclass(frozen=True)
class ApiSettings:
    spec: str | None
    endpoints: str | None
    max_examples: int
    phases: tuple[str, ...]
    workers: int
    timeout_s: float
    error_chars: int
    auth_header: str | None
    auth_prefix: str
    http_timeout_s: float

    @classmethod
    def from_settings(cls, settings: Settings) -> ApiSettings:
        section = settings.section("implement.check.runtime")["api"]
        return cls(section["spec"], section["endpoints"], int(section["maxExamples"]), tuple(section["phases"]),
                   int(section["workers"]), parse_duration(section["timeout"]), int(section["errorChars"]),
                   section["authHeader"], section["authPrefix"], settings.duration("limits.timeouts.http"))


def affected_endpoints(design: Mapping[str, Any], changed: Collection[str],
                       inventory: Sequence[Mapping[str, Any]]) -> list[str]:
    found = [str(item) for item in design.get("affectedEndpoints") or []]
    files = set(changed)
    found += [f"{str(item['method']).upper()} {item['route']}" for item in inventory if item.get("sourceFile") in files]
    return list(dict.fromkeys(found))


def inventory(worktree: Path, relative: str | None) -> list[Mapping[str, Any]]:
    """项目的端点清单；没有配置或读不出时为空(只用方案给出的接口)。"""
    if not relative or not (worktree / relative).is_file():
        return []
    try:
        data = json.loads((worktree / relative).read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [item for item in data if isinstance(item, Mapping)] if isinstance(data, list) else []


def existing(conn: sqlite3.Connection, own_issue: str) -> dict[str, str]:
    """改动前就已存在、未解决、不属于本 Issue 的接口问题：指纹、别名与规范化后的位置 → 问题编号(一次取出，按字典查)。"""
    found: dict[str, str] = {}
    for problem in problems.find(conn):
        if problem.source != SOURCE or problem.status in CLOSED or problem.issue == own_issue:
            continue
        for key in (problem.fingerprint, *problem.extra.get(ALIASES, [])):
            found[key] = problem.id
        if problem.location:
            found[normalize_location(problem.location) or ""] = problem.id
    return found


def fingerprint(failure: report_parser.Failure) -> str:
    """与采集把这条失败转成信号后算出的指纹相同(collect/api_fuzz/mapping.py、collect/dedup/group.py)。"""
    method, route = failure.operation
    status = failure.interaction.status if failure.interaction is not None else None
    signal = Signal(id="", run="", source=SOURCE, check_type=CHECK_TYPE, location=f"{method} {route}", symbol=None,
                    message=failure.title, evidence={"status": status}, occurred_at="", commit=None,
                    environment=None, severity_hint=None, group_key=None, deterministic=True, verified=False,
                    reproducible=False)
    return group.fingerprint(signal, normalize_message(signal.message))


def shallow(endpoints: Sequence[str], *, base_url: str | None, unavailable: str | None, worktree: Path,
            report_dir: Path, settings: ApiSettings, runner: ProcessRunner, environ: Mapping[str, str],
            token: str, transport: Transport, known: Mapping[str, str]) -> Item | None:
    if not endpoints:
        return None
    routes = tuple(dict.fromkeys(endpoint.split(" ", 1)[-1] for endpoint in endpoints))
    command = "schemathesis run " + " ".join(f"--include-path {route}" for route in routes)  # 与实际 argv 一致：每个路由各带一个
    if base_url is None:
        return unverified("api:shallow", CATEGORY, unavailable or "接口服务没有在本机启动")
    problem = invoke.installed_problem()
    if problem is not None:
        return unverified("api:shallow", CATEGORY, problem)
    spec = _spec(settings.spec, worktree, report_dir, base_url, transport, settings.http_timeout_s)
    if isinstance(spec, str):
        return unverified("api:shallow", CATEGORY, spec)
    auth = (settings.auth_header, settings.auth_prefix) if token and settings.auth_header else None
    config = config_writer.write(report_dir / CONFIG_FILE, settings.workers, (), auth)
    params = invoke.RunParameters(max_examples=settings.max_examples, phases=settings.phases, include_methods=(),
                                  include_paths=routes, exclude_regex=None, seed=invoke.random_seed(),
                                  workers=settings.workers, sanitize_keys=(), timeout_s=settings.timeout_s)
    invocation = invoke.run(runner, params, token=token, spec_path=spec, config_file=config, base_url=base_url,
                            report_dir=report_dir, environ=environ)
    failure = invocation.problem()
    if failure is not None:
        return unverified("api:shallow", CATEGORY, failure)
    report = report_parser.parse(report_dir / invoke.EVENTS_FILE, settings.error_chars)
    return judge(report, command, known, (str(report_dir / invoke.EVENTS_FILE),))


def judge(report: report_parser.Report, command: str, known: Mapping[str, str],
          evidence: tuple[str, ...]) -> Item:
    if not report.complete:
        return unverified("api:shallow", CATEGORY, "Schemathesis 的报告不完整：" + "；".join(report.errors))
    introduced: list[str] = []
    before: set[str] = set()
    for failure in report.failures:
        place = f"{failure.operation[0]} {failure.operation[1]}"
        problem_id = known.get(fingerprint(failure)) or known.get(normalize_location(place) or "")
        if problem_id is None:
            introduced.append(f"{place} {failure.title}")
        else:
            before.add(problem_id)
    note = f"改动前已存在的问题 {'、'.join(sorted(before))} 仍出现，不计失败" if before else None
    if introduced:
        reason = "；".join(part for part in (f"浅跑出现新的 5xx：{'；'.join(dict.fromkeys(introduced))}", note) if part)
        return Item("api:shallow", CATEGORY, Result.FAILED, command, evidence, reason)
    return Item("api:shallow", CATEGORY, Result.PASSED, command, evidence, note)


def _spec(spec: str | None, worktree: Path, report_dir: Path, base_url: str, transport: Transport,
          timeout_s: float) -> Path | str:
    if not spec:
        return "没有配置接口描述(implement.check.runtime.api.spec)"
    if not spec.startswith("/"):
        path = worktree / spec
        return path if path.is_file() else f"接口描述 {spec} 不在 worktree 中"
    response = transport(HttpRequest("GET", base_url + spec, timeout_s))
    if not response.ok:
        return f"取接口描述 {spec} 失败：{response.error or response.status}"
    target = report_dir / SPEC_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.body)
    return target
