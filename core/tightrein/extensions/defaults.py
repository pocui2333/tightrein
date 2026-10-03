"""没有扩展时各扩展点的核心默认行为(architecture/10 4.1)。

核心默认不产出任何数据：结果的 output 与 failure 都为空，notes 写明原因；下游按「没有这份数据」降级，
例如 api-fuzz 跳过、接口与页面覆盖率记为未知、不做越权检查、平台来源不启用、只运行 Semgrep 与审查、
需要本机服务的检查记为未验证，这些降级由各采集方法与 verify 实现。扩展报告 not-applicable 时同样按核心默认处理。
"""

from __future__ import annotations

from collections.abc import Iterable

from tightrein.domain.enums import ExtensionLayer, ExtensionPoint
from tightrein.extensions.result import PointResult

DEFAULT_NOTES: dict[ExtensionPoint, str] = {
    ExtensionPoint.SPEC_EXPORT: "未提供 spec-export 扩展",
    ExtensionPoint.AUTHZ_ENDPOINTS: "未提供 authz-endpoints 扩展，不做越权检查",
    ExtensionPoint.AUTHZ_ROLES: "未提供 authz-roles 扩展，不做越权检查",
    ExtensionPoint.ERROR_TRACKING: "未配置错误追踪平台(extensions.error-tracking)",
    ExtensionPoint.LOG_PLATFORM: "未配置集中日志平台(extensions.log-platform)",
    ExtensionPoint.LOG_PARSE: "未提供 log-parse 扩展",
    ExtensionPoint.ALERT_SOURCE: "未配置业务告警来源(extensions.alert-source)",
    ExtensionPoint.STATIC_TOOLS: "未配置确定性工具",
    ExtensionPoint.PAGE_ROUTES: "未提供 page-routes 扩展，页面覆盖率记为未知",
    ExtensionPoint.LOCAL_RUN: "未提供 local-run 扩展",
    ExtensionPoint.DEPLOY_SOURCE: "未配置部署来源(extensions.deploy-source)，以合并时间加观察期为准",
}


def result(point: ExtensionPoint, extra_notes: Iterable[str] = ()) -> PointResult:
    """该扩展点的核心默认结果；extra_notes 放在默认说明之后，例如扩展报告不适用的原因。"""
    return PointResult(point, ExtensionLayer.DEFAULT, notes=(DEFAULT_NOTES[point], *extra_notes))
