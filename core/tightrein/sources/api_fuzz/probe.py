"""ApiFuzzProbe(architecture/04 2.2、2.9)。

步骤：检查 Schemathesis 的安装 → 接口描述 → 越权模型 → 逐个角色取凭证 → 生成 schemathesis.toml → 每个取得凭证的
角色调用一次 Schemathesis → 清除报告中的凭证 → 解析报告 → 过滤与映射。
- 角色取 common/session.probe_roles：没有配置 accounts 时以匿名身份(anonymous)运行，请求不带凭证请求头，
  配置文件为 schemathesis-anonymous.toml(没有 headers 段)；匿名身份不传给 authz-roles，不参与越权检查；
- 状态：全部角色登录失败或全部调用失败为 failed；部分角色失败、越权数据的扩展失败为 partial；
- 角色的调用失败(退出码 2、超时、报告不完整)不产出该角色的信号，也不计入覆盖范围；
- Schemathesis 在 NDJSON 的用例记录中保留了原始请求头，调用结束后把本次使用过的凭证在该角色目录的全部文件中
  替换为 `[已脱敏]`；
- 没有目标地址时为 skipped 并写明原因；检查项按 sources.api-fuzz.checks 分级(checks.py)，生产环境只测 GET 与允许
  清单中的接口，配置违反时启动即报错(limits.py)；
- from_raw 只重新解析原始输出目录中已有的报告，不登录、不调用工具，接口描述与模型取已缓存的文件。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.enums import ExtensionPoint, ProbeLevel, RunStatus
from tightrein.domain.run import Coverage, Endpoint, EnvironmentDetail
from tightrein.domain.signal import Signal
from tightrein.extensions.client import ExtensionClient
from tightrein.observability.redact import REDACTED
from tightrein.sources.api_fuzz import checks, config_writer, invoke, limits, mapping, report_parser, spec
from tightrein.sources.api_fuzz.authz import model as authz
from tightrein.sources.api_fuzz.authz.model import AuthzModel
from tightrein.sources.api_fuzz.invoke import RunParameters, SeedSource, random_seed
from tightrein.sources.base import (PROBE_LEVELS, ProbeOptions, ProbeOutcome, ProbeTarget, failed, resolve_level,
                                     skipped)
from tightrein.sources.common.procs import Launcher
from tightrein.sources.common.raw import RawDir
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.session import ANONYMOUS_ROLE, NO_TARGET, Session, probe_roles
from tightrein.sources.common.signals import RandomBytes, SignalFactory
from tightrein.store.files.layout import WorkspaceLayout

CONFIG_FILE = "schemathesis.toml"
ANONYMOUS_CONFIG_FILE = "schemathesis-anonymous.toml"
ANONYMOUS_ONLY = "只以匿名身份运行，不做越权检查"
AUTHZ_OFF = "越权检查已关闭(sources.api-fuzz.checks.authorization)"


@dataclass(frozen=True)
class ApiFuzzDependencies:
    config: ProjectConfig
    client: ExtensionClient
    session: Session
    launcher: Launcher
    layout: WorkspaceLayout
    redactor: ProbeRedactor
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    randomness: RandomBytes = os.urandom
    seed_source: SeedSource = random_seed
    installed_problem: Callable[[], str | None] = invoke.installed_problem
    command: Path | None = None


def scrub_tokens(directory: Path, tokens: Iterable[str]) -> None:
    """把目录下全部文件中出现的 token 替换为 [已脱敏]。"""
    secrets = [token.encode("utf-8") for token in tokens if token]
    if not secrets or not directory.is_dir():
        return
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        cleaned = data
        for secret in secrets:
            cleaned = cleaned.replace(secret, REDACTED.encode("utf-8"))
        if cleaned != data:
            path.write_bytes(cleaned)


@dataclass
class _Accumulator:
    signals: list[Signal] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    incomplete_roles: list[str] = field(default_factory=list)
    stats: dict[str, float] = field(default_factory=lambda: {
        "rolesTested": 0, "failures": 0, "unjudgedAuthFailures": 0, "expectedDenials": 0, "unknownEvents": 0})
    errors: list[str] = field(default_factory=list)


class ApiFuzzProbe:
    name = ProbeKind.API_FUZZ
    levels = PROBE_LEVELS[ProbeKind.API_FUZZ]

    def __init__(self, dependencies: ApiFuzzDependencies) -> None:
        self.deps = dependencies

    def run(self, target: ProbeTarget, level: ProbeLevel | None, options: ProbeOptions) -> ProbeOutcome:
        resolved = resolve_level(self.name, level)
        if resolved is None:
            raise ValueError("api-fuzz 必须有档位")
        roles = probe_roles(self.deps.config, options.roles)
        if options.from_raw:
            return self._from_raw(target, resolved, options, roles)
        if target.base_url is None:
            return skipped(NO_TARGET)
        allowed = limits.check(self.deps.config)
        problem = self.deps.installed_problem()
        if problem is not None:
            return failed(problem)
        described = spec.ensure(self.deps.client, target.worktree, target.release)
        if described.status is RunStatus.SKIPPED:
            return skipped(described.notes[0], notes=described.notes, extensions=described.extensions)
        if described.document is None or described.path is None or target.release is None:
            return failed(*described.notes, extensions=described.extensions)
        plan = checks.plan(self.deps.config, self.deps.client.method(ExtensionPoint.SPEC_EXPORT))
        modeled = (self._model(target, roles, described.document) if plan.authorization
                   else authz.ModelOutcome(None, notes=(AUTHZ_OFF,)))
        tokens, login_failures = self.deps.session.login_all(roles)
        extensions = {**described.extensions, **modeled.extensions}
        notes = [*described.notes, *modeled.notes]
        notes += [f"角色 {role} 登录失败：{reason}" for role, reason in login_failures.items()]
        if not tokens:
            return failed(*notes, "全部角色登录失败", environment=EnvironmentDetail(failed_roles=tuple(login_failures)),
                          extensions=extensions)
        raw = RawDir(target.raw_dir)
        params = invoke.parameters(resolved, self.deps.config, options, self.deps.seed_source, plan, allowed)
        accumulator = _Accumulator()
        for role, token in tokens.items():
            auth = self.deps.session.auth(role)
            config_file = config_writer.write(raw.path(ANONYMOUS_CONFIG_FILE if auth is None else CONFIG_FILE),
                                              params.workers, params.sanitize_keys, auth)
            role_run = invoke.run_role(
                self.deps.launcher, params, role, token.token, described.path, config_file, target.base_url,
                raw.path(role), modeled.path, self.deps.environ, self.deps.command)
            scrub_tokens(role_run.directory, self.deps.session.tokens())
            problem = role_run.problem()
            if problem is not None:
                accumulator.problems.append(problem)
                continue
            self._collect(target, role, raw, modeled.model, accumulator, params.seed)
        return self._outcome(target, params, described.document, accumulator, notes, login_failures, extensions,
                             modeled.degraded, len(tokens))

    def _model(self, target: ProbeTarget, roles: Iterable[str], document: Mapping[str, Any]) -> authz.ModelOutcome:
        named = [role for role in roles if role != ANONYMOUS_ROLE]
        if not named:
            return authz.ModelOutcome(None, notes=(ANONYMOUS_ONLY,))
        if target.worktree is None or target.release is None:
            return authz.ModelOutcome(None, notes=("没有只读 worktree，不做越权检查",))
        return authz.ensure(self.deps.client, target.worktree, target.release, named, document,
                            self.deps.layout.authz_model(target.release), target.clock)

    def _collect(self, target: ProbeTarget, role: str, raw: RawDir, model: AuthzModel | None,
                 accumulator: _Accumulator, seed: int | None) -> None:
        report = report_parser.parse(raw.path(role) / invoke.EVENTS_FILE,
                                         self.deps.redactor.limits.report_error_chars)
        accumulator.stats["unknownEvents"] += report.unknown
        accumulator.errors += report.errors
        if not report.complete:
            accumulator.problems.append(f"角色 {role}：报告不完整(没有引擎结束事件)")
            accumulator.incomplete_roles.append(role)
            return
        factory = SignalFactory(target, self.name, self.deps.redactor, randomness=self.deps.randomness)
        context = mapping.RoleContext(role, role, target.clock.now(), report.seed or seed, target.release,
                                      self.deps.session.tokens(), self.deps.session.auth(role))
        result = mapping.to_signals(report, context, model, factory, self.deps.redactor, target.base_url or "")
        accumulator.signals += result.signals
        accumulator.endpoints += [Endpoint(method, route, role) for method, route in report.tested]
        accumulator.stats["rolesTested"] += 1
        accumulator.stats["failures"] += len(report.failures)
        accumulator.stats["unjudgedAuthFailures"] += result.unjudged_auth_failures
        accumulator.stats["expectedDenials"] += result.expected_denials

    def _outcome(self, target: ProbeTarget, params: RunParameters, document: Mapping[str, Any],
                 accumulator: _Accumulator, notes: list[str], login_failures: Mapping[str, str],
                 extensions: Mapping[str, Mapping[str, Any]], degraded: bool, attempted: int) -> ProbeOutcome:
        notes = [*notes, *accumulator.problems, *accumulator.errors]
        if accumulator.stats["unknownEvents"]:
            notes.append(f"报告中有 {int(accumulator.stats['unknownEvents'])} 条无法识别的事件或条目，已跳过")
        tested = accumulator.stats["rolesTested"]
        if tested == 0:
            status = RunStatus.FAILED
        elif login_failures or accumulator.problems or degraded:
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.OK
        if status is RunStatus.FAILED and not notes:
            notes.append(f"{attempted} 个角色的 Schemathesis 调用全部失败")
        total = len(spec.selected_operations(document, params.exclude_regex))
        coverage = Coverage(endpoints=tuple(accumulator.endpoints), endpoints_total=total,
                            methods=params.methods) if tested else Coverage(endpoints_total=total)
        stats = {**accumulator.stats, "signals": len(accumulator.signals), "seed": params.seed,
                 "operationsTested": len({(item.method, item.route) for item in accumulator.endpoints})}
        environment = EnvironmentDetail(failed_roles=tuple(login_failures),
                                        report_complete=not accumulator.incomplete_roles)
        return ProbeOutcome(status, tuple(accumulator.signals), coverage, environment, stats,
                            RawDir(target.raw_dir).files(), tuple(notes), extensions)

    def _from_raw(self, target: ProbeTarget, level: ProbeLevel, options: ProbeOptions,
                  roles: Iterable[str]) -> ProbeOutcome:
        raw = RawDir(target.raw_dir)
        release = target.release
        document: Mapping[str, Any] = {}
        model = None
        if release is not None and self.deps.layout.openapi(release).is_file():
            document = json.loads(self.deps.layout.openapi(release).read_text(encoding="utf-8"))
        if release is not None and self.deps.layout.authz_model(release).is_file():
            model = authz.load(self.deps.layout.authz_model(release))
        params = invoke.parameters(level, self.deps.config, options, lambda: 0)
        accumulator = _Accumulator()
        present = [role for role in roles if (raw.path(role) / invoke.EVENTS_FILE).is_file()]
        for role in present:
            self._collect(target, role, raw, model, accumulator, None)
        missing = [f"角色 {role} 没有已有的报告" for role in roles if role not in present]
        accumulator.problems += missing
        return self._outcome(target, params, document, accumulator, ["只重新解析已有的报告"], {}, {}, False,
                             len(present))
