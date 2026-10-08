"""API 模糊测试的采集流程：取接口描述 → 跳过判断 → 健康检查 → 登录 → 调用 Schemathesis → 清除报告中的凭证 → 解析报告
→ 转成信号。只查服务端报错(5xx)。

- 被测地址与环境取 sites.json 的 target.baseUrl、target.environment(各来源共用)；没有目标地址时 skipped 并写明原因；
  健康检查、允许清单与登录方式在 sites.json 的 api_fuzz 下；
- 生产环境只测 GET 与允许清单中的路由，配置违反时启动就报错(写明键名)，一个请求都不发(limits.py)；
- 没有新部署、接口描述也没变(state 表 collect.api_fuzz 记着上次测过的部署 commit 与接口描述哈希)时 skipped：同一次
  部署只跑一遍；
- 目标健康检查不通过时整次跳过、不调用 Schemathesis、不产出信号，避免环境宕机时刷出一批 5xx；读取位置不前进，下次
  再测；没有配置健康检查时在说明中写明；
- Schemathesis 只对交互记录脱敏，实测 NDJSON 的用例记录里保留了原始请求头：每次调用结束后把本次用过的 token 在
  报告目录的所有文件中按字节替换掉；
- 调用失败(退出码 2、超时)或报告不完整：本次 failed，不产出信号，也不计入覆盖；
- 覆盖范围(测到的接口)随结果的 coverage 交给去重，判断「覆盖而没再出现」。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from tightrein.collect.api_fuzz import limits, login, mapping, replay, spec
from tightrein.collect.api_fuzz.login import Credential, LoginFailed, LoginSettings
from tightrein.collect.api_fuzz.schemathesis import config_writer, invoke, report_parser
from tightrein.collect.api_fuzz.schemathesis.invoke import SeedSource, random_seed
from tightrein.collect.api_fuzz.spec import SpecNotApplicable, SpecRequest
from tightrein.collect.common.signals import SignalFactory, SignalLimits, deployments, latest_release, releases
from tightrein.collect.common.source import (
    SourceInvalid,
    SourceMisconfigured,
    SourceResult,
    SourceStatus,
    SourceUnavailable,
    skipped,
)
from tightrein.protocol.handoff import Metrics
from tightrein.protocol.http import HttpRequest, Transport, UrllibTransport
from tightrein.protocol.naming import format_iso, parse_duration
from tightrein.protocol.raw import RawDir, raw_dir
from tightrein.protocol.runtime import Runtime
from tightrein.protocol.security import REDACTED, SECRET_KIND
from tightrein.store.tables import state

SOURCE = "collect.api_fuzz"
STATE_KEY = SOURCE
CONFIG_FILE = "schemathesis.toml"
REPORT_DIR = "report"
NO_TARGET = "没有目标地址：在 sites.json 的 target.baseUrl 写被测服务的地址"
NO_METHOD = "接入清单 collect.api_fuzz 没有选接口描述的方法(openapi_file 或 openapi_url)"
UNCHANGED = "没有新部署，接口描述也没有变化，同一次部署只测一遍"
NO_HEALTH = "没有配置健康检查(sites.json 的 api_fuzz.health)，目标宕机时会刷出一批 5xx"
SCRUBBED = REDACTED.format(kind=SECRET_KIND).encode("utf-8")

InstalledProblem = Callable[[], str | None]


def collect(runtime: Runtime, *, transport: Transport | None = None, seed_source: SeedSource = random_seed,
            installed_problem: InstalledProblem = invoke.installed_problem,
            command: Path | None = None) -> SourceResult:
    target = limits.Target.from_sites(runtime.settings.sites)
    base_url, environment = target.base_url, target.environment
    if not base_url:
        return skipped(SOURCE, NO_TARGET)
    sites: Mapping[str, Any] = runtime.settings.sites.get(limits.SITE) or {}
    section = runtime.settings.section(SOURCE)
    try:
        restriction = limits.check(environment, section["includeMethods"], sites.get("allow") or ())
    except limits.ProductionLimitViolated as error:
        raise SourceMisconfigured(str(error)) from error
    problem = installed_problem()
    if problem is not None:
        raise SourceUnavailable(problem)
    method = runtime.setup.module(SOURCE).method
    if not method:
        raise SourceMisconfigured(NO_METHOD)
    sender = transport or UrllibTransport()
    raw = RawDir(raw_dir(runtime.workspace, runtime.run, SOURCE))
    deploy = latest_release(deployments(runtime.conn))
    try:
        described = spec.ensure(method=method, settings=runtime.settings, secrets=runtime.secrets,
                                request=SpecRequest({}, runtime.git, runtime.workspace.root, deploy, sender),
                                cache_dir=runtime.workspace.cache_dir, raw_dir=raw.root)
    except SpecNotApplicable as error:
        return skipped(SOURCE, str(error))
    tested = {"deploy": deploy, "specHash": described.hash}
    previous = state.get(runtime.conn, STATE_KEY)
    if isinstance(previous, Mapping) and all(previous.get(key) == value for key, value in tested.items()):
        return skipped(SOURCE, UNCHANGED)
    timeout_s = runtime.settings.duration("limits.timeouts.http")
    notes = [f"接口描述：{described.label}" + ("(缓存)" if described.cached else "")]
    health = sites.get("health")
    if health:
        failure = check_health(login.join_url(base_url, health) if not health.startswith(("http://", "https://"))
                               else health, sender, parse_duration(section["healthTimeout"]))
        if failure is not None:
            return skipped(SOURCE, f"目标健康检查未通过({failure})，本次不测，下次再试")
    else:
        notes.append(NO_HEALTH)
    try:
        credential = login.login(LoginSettings.from_sites(sites), base_url=base_url, secrets=runtime.secrets,
                                 transport=sender, redactor=runtime.redactor, timeout_s=timeout_s)
    except LoginFailed as error:
        raise SourceUnavailable(error.reason) from error
    replay.remember(runtime.run, credential)
    return _fuzz(runtime, base_url, environment, restriction, described, credential, raw, tested, notes,
                 seed_source, command)


def check_health(url: str, transport: Transport, timeout_s: float) -> str | None:
    """健康检查不通过的原因；通过为 None。"""
    response = transport(HttpRequest("GET", url, timeout_s))
    if response.status is None:
        return f"{url} 没有响应：{response.error}"
    if not response.ok:
        return f"{url} 返回 {response.status}"
    return None


def scrub_tokens(directory: Path, tokens: Iterable[str]) -> None:
    """把目录下全部文件中出现的 token 按字节替换掉(Schemathesis 的 NDJSON 用例记录保留了原始请求头)。"""
    secrets = [token.encode("utf-8") for token in tokens if token]
    if not secrets or not directory.is_dir():
        return
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        cleaned = data
        for secret in secrets:
            cleaned = cleaned.replace(secret, SCRUBBED)
        if cleaned != data:
            path.write_bytes(cleaned)


def _fuzz(runtime: Runtime, base_url: str, environment: str | None, restriction: limits.Restriction,
          described: spec.Spec, credential: Credential | None, raw: RawDir, tested: dict[str, Any],
          notes: list[str], seed_source: SeedSource, command: Path | None) -> SourceResult:
    section = runtime.settings.section(SOURCE)
    params = invoke.parameters(runtime.settings, restriction, seed_source)
    auth = None if credential is None else credential.auth
    config_file = config_writer.write(raw.path(CONFIG_FILE), params.workers, params.sanitize_keys, auth)
    report_dir = raw.path(REPORT_DIR)
    invocation = invoke.run(runtime.runner, params, token="" if credential is None else credential.token,
                            spec_path=described.path, config_file=config_file, base_url=base_url,
                            report_dir=report_dir, environ=runtime.environ, command=command,
                            redactor=runtime.redactor)
    scrub_tokens(report_dir, () if credential is None else (credential.token,))
    problem = invocation.problem()
    if problem is not None:
        raise SourceUnavailable(problem)
    report = report_parser.parse(report_dir / invoke.EVENTS_FILE, int(section["reportErrorChars"]))
    if not report.complete:
        raise SourceInvalid(f"Schemathesis 的报告不完整(没有引擎结束事件)，见 {REPORT_DIR}/{invoke.EVENTS_FILE}")
    signal_limits = SignalLimits.from_settings(runtime.settings, SOURCE)
    factory = SignalFactory(run=runtime.run, source=SOURCE, clock=runtime.clock, redactor=runtime.redactor, raw=raw,
                            limits=signal_limits)
    context = mapping.MappingContext(
        run=runtime.run, finished_at=runtime.clock.now(), seed=report.seed or params.seed, base_url=base_url,
        auth=auth, environment=environment, report_path=REPORT_DIR, body_bytes=signal_limits.evidence_bytes,
        release_at=releases(runtime.conn))
    signals = mapping.to_signals(report, context, factory, runtime.redactor)
    total = len(spec.operations(described.document, params.exclude_regex))
    notes += [f"测到 {len(report.tested)}/{total} 个接口，随机种子 {context.seed}", *report.errors]
    if report.unknown:
        notes.append(f"报告中有 {report.unknown} 条无法识别的事件或条目，已跳过")
    saved = {STATE_KEY: {**tested, "testedAt": format_iso(runtime.clock.now())}}
    return SourceResult(SOURCE, SourceStatus.DONE, signals, len(report.tested), None, None, saved,
                        Metrics(produced={"signals": len(signals), "failures": len(report.failures)}),
                        coverage=[f"{method} {route}" for method, route in report.tested], notes=notes)
