"""api-fuzz 按环境限制可测的请求(redesign/01-collect.md 第 5 节)。

target.environment 为 production 时只测只读方法(GET)，且只测 sources.api-fuzz.production.allow 列出的接口路由；
各档位的 includeMethod 不是 GET 或允许清单为空时启动即报错(ConfigError，写明键名)，不发出任何请求。
"""

from __future__ import annotations

from tightrein.config.project import ConfigError, ConfigIssue, ProjectConfig
from tightrein.domain.enums import ProbeLevel

PRODUCTION = "production"
GET = "GET"
ALLOW = "sources.api-fuzz.production.allow"


def production(config: ProjectConfig) -> bool:
    return config.get("target.environment") == PRODUCTION


def check(config: ProjectConfig) -> tuple[str, ...]:
    """生产环境允许测试的路由；不是生产环境时为空(不限制)。配置违反限制时抛出 ConfigError。"""
    if not production(config):
        return ()
    issues = []
    for level in (ProbeLevel.SHALLOW, ProbeLevel.DEEP):
        key = f"sources.api-fuzz.levels.{level.value}.includeMethod"
        if config.get(key) != GET:
            issues.append(ConfigIssue(key, "生产环境只能测只读方法，须为 GET"))
    allowed = tuple(config.get(ALLOW))
    if not allowed:
        issues.append(ConfigIssue(ALLOW, "生产环境须列出允许测试的接口路由"))
    if issues:
        raise ConfigError(config.path, issues)
    return allowed
