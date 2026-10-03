"""建立复现检查(architecture/07 4.3，design 5.6)。

有确定性来源的由核心直接生成，期望按检查类型：
| 探针 | 生成方式 |
|---|---|
| api-fuzz | 取信号中的请求与角色；5xx 类期望「不属于 5xx」，越权类期望「属于 401、403、404」，契约类期望「不属于 5xx 且
  响应体符合接口描述中的 schema」 |
| static | Semgrep 的命中引用巡检规则(sources.static.semgrep.configs 与规则编号)，期望在该文件中不再命中 |
其余(内部错误、业务告警、访问日志、项目探针、incidental、agent 审查给出的静态问题、没有请求的信号)没有确定性来源，
只有修复第 5 步写的
复现测试(steps/repro_test.py，测试类检查，登记副本为 `<检查编号><后缀>`，放进修复 worktree 见 manifest.place_tests)。

复现检查写在 regressions/<Issue 编号>/，由核心写入，已存在时直接复用；每条检查在 regressions 表登记哈希。
requires 取 local-run 扩展在对应模式下给出的服务名(没有扩展时为空)。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.config.project import MissingSetting, ProjectConfig
from tightrein.domain.enums import Probe, RegressionKind
from tightrein.domain.problem import Problem, role_of
from tightrein.domain.signal import Signal
from tightrein.extensions.client import MODE_API
from tightrein.sources.api_fuzz.mapping import SERVER_ERROR_CHECK, STATUS_CHECK, UNAUTHORIZED_CHECK
from tightrein.sources.api_fuzz.replay import RecordedRequest
from tightrein.sources.common.raw import RawDir
from tightrein.pipeline.checks.regressions import manifest
from tightrein.pipeline.checks.regressions.manifest import CHECKLIST, EXPECT_SUFFIX, ManifestInvalid
from tightrein.store.files import atomic, yaml_text
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import problems, regressions, signals
from tightrein.store.repos.regressions import RegressionCheck

DENIED_STATUSES = (401, 403, 404)
SERVER_ERROR = "5xx"
SUCCESS_STATUSES = ("200", "201")


@dataclass(frozen=True)
class ReproFiles:
    manifest: dict[str, Any]
    files: dict[str, str] = field(default_factory=dict)
    source: str = "draft"


def latest_signal(conn: sqlite3.Connection, problem_id: str) -> Signal | None:
    found = signals.get_many(conn, problems.signal_ids(conn, problem_id))
    return max(found, key=lambda item: item.occurred_at) if found else None


def _checklist(issue_id: str, fingerprints: Sequence[str], check: dict[str, Any]) -> dict[str, Any]:
    return {"issue": issue_id, "problems": list(fingerprints), "checks": [check]}


def _body_schema(layout: WorkspaceLayout, release: str | None, method: str, template: str) -> dict[str, Any] | None:
    """接口描述中该端点成功响应的 schema，带上 components 以解析 #/components/... 的引用。"""
    if release is None or not layout.openapi(release).is_file():
        return None
    spec = json.loads(layout.openapi(release).read_text(encoding="utf-8"))
    responses = spec.get("paths", {}).get(template, {}).get(method.lower(), {}).get("responses", {})
    for status in SUCCESS_STATUSES:
        schema = (((responses.get(status) or {}).get("content") or {}).get("application/json") or {}).get("schema")
        if schema is not None:
            return {"allOf": [schema], "components": spec.get("components", {})}
    return None


def from_api_fuzz(issue_id: str, fingerprints: Sequence[str], signal: Signal, *, layout: WorkspaceLayout,
                  requires: Sequence[str]) -> ReproFiles | None:
    request = (signal.context or {}).get("request")
    role = role_of(signal)
    if not request or role is None:
        return None
    recorded = RecordedRequest.from_context(request, RawDir(layout.probe_raw_dir(signal.run_id, Probe.API_FUZZ)))
    expect: dict[str, Any]
    if signal.check == SERVER_ERROR_CHECK:
        expect = {"status": {"notIn": [SERVER_ERROR]}}
    elif signal.check == UNAUTHORIZED_CHECK:
        expect = {"status": {"in": list(DENIED_STATUSES)}}
    elif signal.check == STATUS_CHECK:
        schema = _body_schema(layout, signal.release, recorded.method, recorded.path_template)
        if schema is None:
            return None
        expect = {"status": {"notIn": [SERVER_ERROR]}, "bodySchema": schema}
    else:
        return None
    check = {"id": "api-1", "kind": RegressionKind.API.value, "role": role, "file": "api-1.request.json",
             "location": signal.location, "requires": list(requires), "precondition": None}
    body = {"method": recorded.method, "path": recorded.path, "pathTemplate": recorded.path_template,
            "query": dict(recorded.query), "body": recorded.body}
    files = {"api-1.request.json": json.dumps(body, ensure_ascii=False, indent=2) + "\n",
             f"api-1{EXPECT_SUFFIX}": json.dumps(expect, ensure_ascii=False, indent=2) + "\n"}
    return ReproFiles(_checklist(issue_id, fingerprints, check), files, Probe.API_FUZZ.value)


def from_static(issue_id: str, fingerprints: Sequence[str], signal: Signal, *,
                config: ProjectConfig) -> ReproFiles | None:
    finding = (signal.context or {}).get("toolFinding") or {}
    try:
        configs = list(config.get("sources.static.semgrep.configs"))
    except MissingSetting:
        return None
    if finding.get("tool") != "semgrep" or not finding.get("file"):
        return None
    reference = yaml_text.dump({"configs": configs, "rule": finding["rule"]})
    check = {"id": "static-1", "kind": RegressionKind.STATIC.value, "role": None, "file": "static-1.yaml",
             "location": signal.location, "targets": [finding["file"]], "requires": [], "precondition": None}
    return ReproFiles(_checklist(issue_id, fingerprints, check), {"static-1.yaml": reference}, Probe.STATIC.value)


def _for_problem(problem: Problem, signal: Signal, issue_id: str, fingerprints: Sequence[str], *,
                 layout: WorkspaceLayout, config: ProjectConfig,
                 services: Mapping[str, Sequence[str]]) -> ReproFiles | None:
    if problem.probe is Probe.API_FUZZ:
        return from_api_fuzz(issue_id, fingerprints, signal, layout=layout, requires=services.get(MODE_API, ()))
    if problem.probe is Probe.STATIC:
        return from_static(issue_id, fingerprints, signal, config=config)
    return None


def generate(conn: sqlite3.Connection, layout: WorkspaceLayout, config: ProjectConfig, issue_id: str,
             problem_ids: Sequence[str], fingerprints: Sequence[str],
             services: Mapping[str, Sequence[str]]) -> ReproFiles | None:
    """按第一个有确定性来源的关联问题生成；都没有时返回 None，由 fix-scout 给出草稿。"""
    for problem_id in problem_ids:
        problem = problems.get(conn, problem_id)
        if problem is None:
            continue
        signal = latest_signal(conn, problem.id)
        found = None if signal is None else _for_problem(problem, signal, issue_id, fingerprints, layout=layout,
                                                          config=config, services=services)
        if found is not None:
            return found
    return None


def existing(conn: sqlite3.Connection, issue_id: str) -> list[RegressionCheck]:
    return regressions.find(conn, issue_id=issue_id)


def write(conn: sqlite3.Connection | None, directory: Path, relative: str, repro: ReproFiles) -> list[RegressionCheck]:
    """写入清单与检查文件，返回每条检查(带哈希)；conn 给出时登记 regressions 表(--output 模式不登记)。"""
    for name, text in repro.files.items():
        atomic.write_text(directory / name, text)
    atomic.write_text(directory / CHECKLIST, yaml_text.dump(repro.manifest))
    try:
        loaded = manifest.load(directory)
    except ManifestInvalid as error:
        raise ValueError(f"写入的复现检查不合格：{error}") from error
    checks = [RegressionCheck(loaded.issue, entry.id, entry.kind, relative, manifest.entry_hash(directory, entry),
                              requires=entry.requires) for entry in loaded.checks]
    if conn is not None:
        for check in checks:
            regressions.save(conn, check)
    return checks

