"""各扩展点的固定属性(architecture/10 2.6、2.7 与第 3 章)：是否按 commit 缓存、是否允许 extend、
请求中是否带仓库与 commit，以及输入输出 schema 的名称。

扩展点的枚举 ExtensionPoint 与其他枚举一起定义在 domain/enums.py(中文名称集中在那里)，这里按扩展点登记属性。
"""

from __future__ import annotations

from dataclasses import dataclass

from tightrein.config.layers import core_value
from tightrein.domain.enums import ExtensionPoint

PROTOCOL = 1
REQUEST_SCHEMA = "extension/extension-request.schema.json"
RESPONSE_SCHEMA = "extension/extension-response.schema.json"
STACK_MANIFEST_SCHEMA = "extension/stack-manifest.schema.json"


@dataclass(frozen=True)
class PointSpec:
    point: ExtensionPoint
    cached: bool
    extendable: bool
    uses_repo: bool

    @property
    def timeout_seconds(self) -> int:
        """缺省超时取自 defaults.yaml 的 runtime.extensions.pointTimeoutSeconds。"""
        return int(core_value(f"runtime.extensions.pointTimeoutSeconds.{self.point.value}"))

    @property
    def input_schema(self) -> str:
        return f"extension/points/{self.point.value}.input.schema.json"

    @property
    def output_schema(self) -> str:
        return f"extension/points/{self.point.value}.output.schema.json"


SPECS: dict[ExtensionPoint, PointSpec] = {
    item.point: item
    for item in (
        PointSpec(ExtensionPoint.SPEC_EXPORT, cached=True, extendable=False, uses_repo=True),
        PointSpec(ExtensionPoint.AUTHZ_ENDPOINTS, cached=True, extendable=False, uses_repo=True),
        PointSpec(ExtensionPoint.AUTHZ_ROLES, cached=True, extendable=False, uses_repo=True),
        PointSpec(ExtensionPoint.ERROR_TRACKING, cached=False, extendable=False, uses_repo=False),
        PointSpec(ExtensionPoint.LOG_PLATFORM, cached=False, extendable=False, uses_repo=False),
        PointSpec(ExtensionPoint.LOG_PARSE, cached=False, extendable=True, uses_repo=False),
        PointSpec(ExtensionPoint.ALERT_SOURCE, cached=False, extendable=False, uses_repo=False),
        PointSpec(ExtensionPoint.STATIC_TOOLS, cached=False, extendable=True, uses_repo=True),
        PointSpec(ExtensionPoint.PAGE_ROUTES, cached=True, extendable=False, uses_repo=True),
        PointSpec(ExtensionPoint.LOCAL_RUN, cached=False, extendable=True, uses_repo=True),
        PointSpec(ExtensionPoint.DEPLOY_SOURCE, cached=False, extendable=False, uses_repo=True),
    )
}


# 命令中的 {python} 在执行前替换为核心虚拟环境的解释器(architecture/10 1.6)。
PYTHON_PLACEHOLDER = "{python}"
