"""api-fuzz 的检查项分级(redesign/01-collect.md 第 5 节)：sources.api-fuzz.checks 中每项一个开关。

- serverError(not_a_server_error)：开启；
- authorization(自定义的 unauthorized_role_access 与 ignored_auth，需要角色能力表)：开启；
- statusCode(status_code_conformance、negative_data_rejection、positive_data_acceptance、missing_required_header、
  use_after_free、ensure_resource_availability)：开启；
- responseSchema(response_schema_conformance、content_type_conformance、response_headers_conformance)：auto，接口描述
  由框架导出时开启，手写(core/openapi-file)时关闭；
- responseTime(max_response_time，阈值 thresholds.slowResponseSeconds)：关闭；
- unsupportedMethod(unsupported_method、allow_header_conformance)：关闭。

关闭的检查以 --exclude-checks 传给 Schemathesis；authorization 关闭时不加载越权模型；responseTime 关闭时不传
--max-response-time。
"""

from __future__ import annotations

from dataclasses import dataclass

from tightrein.config.project import ProjectConfig

SETTINGS = "sources.api-fuzz.checks"
AUTO = "auto"
HANDWRITTEN_SPEC = "core/openapi-file"
GROUPS: dict[str, tuple[str, ...]] = {
    "serverError": ("not_a_server_error",),
    "authorization": ("ignored_auth",),
    "statusCode": ("status_code_conformance", "negative_data_rejection", "positive_data_acceptance",
                   "missing_required_header", "use_after_free", "ensure_resource_availability"),
    "responseSchema": ("response_schema_conformance", "content_type_conformance", "response_headers_conformance"),
    "responseTime": (),
    "unsupportedMethod": ("unsupported_method", "allow_header_conformance"),
}


@dataclass(frozen=True)
class CheckPlan:
    enabled: frozenset[str]

    @property
    def excluded(self) -> tuple[str, ...]:
        return tuple(name for group, names in GROUPS.items() if group not in self.enabled for name in names)

    @property
    def authorization(self) -> bool:
        return "authorization" in self.enabled

    @property
    def response_time(self) -> bool:
        return "responseTime" in self.enabled


def plan(config: ProjectConfig, spec_method: str | None) -> CheckPlan:
    """spec_method 为 spec-export 选用的方法编号；不是手写接口描述的方法时 auto 视为开启。"""
    values = config.get(SETTINGS)
    enabled = set()
    for group in GROUPS:
        value = values.get(group, False)
        if value == AUTO:
            value = spec_method is not None and spec_method != HANDWRITTEN_SPEC
        if value:
            enabled.add(group)
    return CheckPlan(frozenset(enabled))
