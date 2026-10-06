"""越权检查的数据(architecture/04 2.3、2.7)：合并 authz-endpoints 与 authz-roles 的输出，写出
data/specs/<commit>/authz-model.json。

- 端点按「方法 + 路由模板」与接口描述的 paths 对齐：先按原文，再不区分大小写，对齐后路由写成接口描述中的写法；
  对不上的端点、requires 中出现而角色数据的 capabilities 中没有的能力所涉及的端点、角色数据中缺少的角色，
  都列入 notes，不参与越权检查；
- 两个扩展任一没有实现(或报告不适用)时没有模型，不做越权检查；任一失败时同样没有模型，并标记 degraded，
  探针据此把状态记为 partial；
- 模型文件按 data/authz-model.schema.json 校验后写出，由 hooks 在 Schemathesis 进程中读取；每次运行重新合成，
  两份扩展输出本身按 commit 缓存。
- missing_capabilities 返回某角色访问某端点所缺的能力：端点或角色不在模型中时为空(无法判断)，匿名端点与
  不需要能力的端点为空元组。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.contracts import validate
from tightrein.domain.clock import Clock, format_iso
from tightrein.extensions.client import ExtensionClient, WorktreeNotAtCommit
from tightrein.extensions.result import PointResult
from tightrein.sources.api_fuzz.spec import operations
from tightrein.store.files import atomic

SCHEMA = "data/authz-model.schema.json"


@dataclass(frozen=True)
class EndpointRule:
    requires: tuple[str, ...]
    anonymous: bool


@dataclass(frozen=True)
class AuthzModel:
    commit: str
    endpoints: Mapping[tuple[str, str], EndpointRule]
    capabilities: tuple[str, ...]
    roles: Mapping[str, Mapping[str, bool]]

    def rule(self, method: str, route: str) -> EndpointRule | None:
        return self.endpoints.get((method.upper(), route))

    def role_capabilities(self, role: str) -> tuple[str, ...]:
        return tuple(sorted(name for name, granted in self.roles.get(role, {}).items() if granted))

    def missing_capabilities(self, role: str, method: str, route: str) -> tuple[str, ...] | None:
        rule = self.rule(method, route)
        if rule is None or role not in self.roles:
            return None
        if rule.anonymous:
            return ()
        granted = self.roles[role]
        return tuple(name for name in rule.requires if not granted.get(name, False))

    def to_document(self, generated_at: str) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "generatedAt": generated_at,
            "endpoints": [
                {"method": method, "route": route, "requires": list(rule.requires), "anonymous": rule.anonymous}
                for (method, route), rule in sorted(self.endpoints.items(), key=lambda item: (item[0][1], item[0][0]))
            ],
            "capabilities": list(self.capabilities),
            "roles": {role: dict(sorted(granted.items())) for role, granted in sorted(self.roles.items())},
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> AuthzModel:
        validate.check(SCHEMA, document)
        endpoints = {(item["method"], item["route"]): EndpointRule(tuple(item["requires"]), item["anonymous"])
                     for item in document["endpoints"]}
        return cls(document["commit"], endpoints, tuple(document["capabilities"]),
                   {role: dict(granted) for role, granted in document["roles"].items()})


def load(path: Path) -> AuthzModel:
    return AuthzModel.from_document(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class ModelOutcome:
    model: AuthzModel | None
    path: Path | None = None
    notes: tuple[str, ...] = ()
    degraded: bool = False
    extensions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def _route_index(spec: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for method, route in operations(spec):
        index.setdefault((method, route.lower()), route)
    return index


def merge(commit: str, endpoints: Mapping[str, Any], roles_output: Mapping[str, Any], roles: Sequence[str],
          spec: Mapping[str, Any]) -> tuple[AuthzModel | None, list[str]]:
    notes: list[str] = []
    known = set(operations(spec))
    index = _route_index(spec)
    capabilities = tuple(roles_output["capabilities"])
    capability_set = set(capabilities)
    aligned: dict[tuple[str, str], EndpointRule] = {}
    unmatched: list[str] = []
    unknown: dict[str, list[str]] = {}
    for item in endpoints["endpoints"]:
        key = (item["method"], item["route"])
        route = item["route"] if key in known else index.get((item["method"], item["route"].lower()))
        if route is None:
            unmatched.append(f"{item['method']} {item['route']}")
            continue
        missing = [name for name in item["requires"] if name not in capability_set]
        if missing and not item["anonymous"]:
            for name in missing:
                unknown.setdefault(name, []).append(f"{item['method']} {route}")
            continue
        aligned[(item["method"], route)] = EndpointRule(tuple(item["requires"]), item["anonymous"])
    if unmatched:
        notes.append(f"{len(unmatched)} 个端点在接口描述中找不到，不做越权检查：{'、'.join(sorted(unmatched))}")
    for name, where in sorted(unknown.items()):
        notes.append(f"能力 {name} 不在 authz-roles 的能力清单中，涉及的端点不做越权检查：{'、'.join(sorted(where))}")
    for unresolved in endpoints.get("unresolved", []):
        notes.append(f"authz-endpoints 无法解析 {unresolved['symbol']}：{unresolved['reason']}")
    role_map = {role: dict(roles_output["roles"][role]) for role in roles if role in roles_output["roles"]}
    for role in roles:
        if role not in role_map:
            notes.append(f"authz-roles 的输出中没有角色 {role}，该角色不做越权检查")
    if not role_map:
        return None, notes
    return AuthzModel(commit, aligned, capabilities, role_map), notes


def _call(call: Callable[..., PointResult], *args: Any) -> tuple[PointResult | None, str | None]:
    try:
        return call(*args), None
    except WorktreeNotAtCommit as error:
        return None, f"{error}；先执行 tightrein project worktree sync --commit {error.commit}"


def ensure(client: ExtensionClient, repo: Path, release: str, roles: Sequence[str], spec: Mapping[str, Any],
           output: Path, clock: Clock) -> ModelOutcome:
    endpoints, problem = _call(client.authz_endpoints, repo, release)
    if problem is not None or endpoints is None:
        return ModelOutcome(None, notes=(f"不做越权检查：{problem}",), degraded=True)
    role_result, problem = _call(client.authz_roles, repo, release, list(roles))
    if problem is not None or role_result is None:
        return ModelOutcome(None, notes=(f"不做越权检查：{problem}",), degraded=True)
    results = (endpoints, role_result)
    entries = {result.point.value: result.stats_entry() for result in results}
    notes = [note for result in results for note in result.notes]
    failed = [f"{result.point.value} 失败，不做越权检查：{result.failure.describe()}"
              for result in results if result.failure is not None]
    if failed:
        return ModelOutcome(None, notes=tuple(notes + failed), degraded=True, extensions=entries)
    if endpoints.output is None or role_result.output is None:
        return ModelOutcome(None, notes=tuple(notes), extensions=entries)
    model, merged = merge(release, endpoints.output, role_result.output, roles, spec)
    notes += merged
    if model is None:
        return ModelOutcome(None, notes=tuple(notes), extensions=entries)
    document = model.to_document(format_iso(clock.now()))
    validate.check(SCHEMA, document)
    atomic.write_text(output, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return ModelOutcome(model, output, tuple(notes), extensions=entries)
