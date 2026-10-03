"""采集方法的启用判断(redesign/01-collect.md 第 0 节)：每种方法默认不启用，配置了才启用。

静态巡检与任务外发现只依赖仓库与工作区，总是可用；其余方法给出未启用的原因，写进 collect 与编排的运行摘要。
"""

from __future__ import annotations

from collections.abc import Callable

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import ExtensionPoint, Probe

Configured = Callable[[ExtensionPoint], bool]


def _sources(config: ProjectConfig, name: str) -> dict:
    return config.data.get("sources", {}).get(name) or {}


def disabled(config: ProjectConfig, configured: Configured) -> dict[str, str]:
    """未启用的方法 → 原因；configured 判断扩展点是否由技术栈或项目实现(ExtensionClient.configured)。"""
    found: dict[str, str] = {}
    log_platform = configured(ExtensionPoint.LOG_PLATFORM)
    if not configured(ExtensionPoint.ERROR_TRACKING) and not (log_platform and _sources(config, "platform-errors")
                                                              .get("logQuery")):
        found[Probe.PLATFORM_ERRORS.value] = "没有配置 extensions.error-tracking 或 extensions.log-platform 与 logQuery"
    if not (log_platform and _sources(config, "access-log").get("query")):
        found[Probe.ACCESS_LOG.value] = "没有配置 sources.access-log.query 与 extensions.log-platform"
    if not configured(ExtensionPoint.ALERT_SOURCE):
        found[Probe.ALERTS.value] = "没有配置 extensions.alert-source"
    if not config.data.get("sources", {}).get("project-probes"):
        found[Probe.PROJECT_PROBE.value] = "sources.project-probes 没有登记探针"
    if config.base_url is None:
        found[Probe.API_FUZZ.value] = "没有配置 target.baseUrl"
    elif not configured(ExtensionPoint.SPEC_EXPORT):
        found[Probe.API_FUZZ.value] = "没有配置接口描述(extensions.spec-export)"
    return found
