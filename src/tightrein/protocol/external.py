"""外部依赖(平台接口、项目脚本)失败的分类：各模块只抛这几种带类型的错误，由调用方一处归类(limits.md「按失败类型处理」)。

采集、实施的本机运行检查、发布读部署记录都经 protocol/http.py、protocol/scripts.py 访问外部，失败统一落在这里的几类。
"""

from __future__ import annotations

from enum import StrEnum


class FailureKind(StrEnum):
    UNAVAILABLE = "unavailable"  # 连不上、非 2xx、认证失败、脚本退出码非 0
    INVALID = "invalid"  # 返回内容不合格：不是 JSON、结构不对、输出不合契约
    MISCONFIGURED = "misconfigured"  # 配置缺项或不合方法清单
    TIMEOUT = "timeout"  # 超过时限


class ExternalError(Exception):
    kind: FailureKind = FailureKind.UNAVAILABLE

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Unavailable(ExternalError):
    kind = FailureKind.UNAVAILABLE


class Invalid(ExternalError):
    kind = FailureKind.INVALID


class Misconfigured(ExternalError):
    kind = FailureKind.MISCONFIGURED
