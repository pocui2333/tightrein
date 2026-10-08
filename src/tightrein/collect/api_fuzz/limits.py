"""被测目标，与生产环境的限制：生产环境只测 GET，且只测允许清单中的接口路由。

被测地址与环境取 sites.json 的 target.baseUrl、target.environment(与项目探针等其他来源共用一处)；本模块自己的
健康检查、允许清单与登录方式在 sites.json 的 api_fuzz 下。
被测环境为 production 时：方法限定写的不是只有 GET、或允许清单为空，启动就报错(写明键名)，一个请求都不发；
不是生产环境时不限制。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

TARGET_SITE = "target"
SITE = "api_fuzz"
PRODUCTION = "production"
GET = "GET"
METHODS_KEY = "controls.collect.api_fuzz.includeMethods"
ALLOW_KEY = "sites.api_fuzz.allow"


class ProductionLimitViolated(Exception):
    """配置违反生产环境的限制；problems 每条带键名。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("生产环境的接口模糊测试配置不合规：" + "；".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Target:
    base_url: str | None
    environment: str | None

    @classmethod
    def from_sites(cls, sites: Mapping[str, Any]) -> Target:
        found = sites.get(TARGET_SITE) or {}
        return cls(found.get("baseUrl") or None, found.get("environment") or None)


@dataclass(frozen=True)
class Restriction:
    methods: tuple[str, ...]  # 为空时不限方法
    paths: tuple[str, ...]  # 为空时不限路由


def check(environment: str | None, methods: Sequence[str], allow: Sequence[str]) -> Restriction:
    """返回实际可测的方法与路由；生产环境违反限制时抛出 ProductionLimitViolated。"""
    normalized = tuple(method.upper() for method in methods)
    if environment != PRODUCTION:
        return Restriction(normalized, ())
    problems = []
    if normalized != (GET,):
        problems.append(f"{METHODS_KEY}：生产环境只能测只读方法，须为 [\"GET\"]")
    if not allow:
        problems.append(f"{ALLOW_KEY}：生产环境须列出允许测试的接口路由")
    if problems:
        raise ProductionLimitViolated(problems)
    return Restriction((GET,), tuple(allow))
