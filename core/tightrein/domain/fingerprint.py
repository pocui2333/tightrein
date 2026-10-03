"""指纹(redesign/02-aggregate.md 第 1 节)。

来自错误追踪、监控平台的信号直接使用平台的分组编号(context.platformGroup，写成「<平台>:<编号>」)；其余来源只用稳定的
逻辑位置字段(来源与规则、文件、符号、归一化后的消息，不用行号)拼成规范字符串，取 SHA-1 前 16 位。项目探针在输出中
自行给出指纹，与探针名一起计算。
"""

from __future__ import annotations

import hashlib

from tightrein.domain.enums import Probe
from tightrein.domain.normalize import location as normalize_location
from tightrein.domain.normalize import symbol
from tightrein.domain.signal import Signal

CURRENT_VERSION = 1
AUTHZ_CHECK = "unauthorized_role_access"
REGRESSION_CHECK = "regression"
FRAME_COUNT = 3
PLATFORM_GROUP = "platformGroup"


def _status_class(status: object) -> str:
    if not isinstance(status, int):
        return "none"
    return f"{status // 100}xx"


def _require_normalized(signal: Signal) -> str:
    if signal.normalized_message is None:
        raise ValueError(f"信号 {signal.id} 缺少规范化后的消息，无法计算指纹")
    return signal.normalized_message


def _api_parts(location: str) -> list[str]:
    method, _, route = normalize_location(location).partition(" ")
    return [method, route]


def _parts(signal: Signal) -> list[str]:
    probe, check = signal.probe, signal.check
    if probe is Probe.API_FUZZ:
        parts = [check, *_api_parts(signal.location), _status_class(signal.ctx("response", "status"))]
        if check == AUTHZ_CHECK:
            parts.append(str(signal.ctx("role")))
        return parts
    if probe is Probe.PLATFORM_ERRORS:
        frames = signal.ctx("projectFrames") or []
        if frames:
            return [str(signal.ctx("exceptionType")), *(symbol(f) for f in frames[:FRAME_COUNT])]
        return [str(signal.ctx("category")), _require_normalized(signal)]
    if probe is Probe.PROJECT_PROBE:
        return [str(signal.ctx("sourceName")), str(signal.ctx("probeFingerprint"))]
    if probe is Probe.ACCESS_LOG:
        return [check, normalize_location(signal.location)]
    if probe is Probe.STATIC:
        return [check, normalize_location(signal.location)]
    if probe is Probe.INCIDENTAL:
        return [normalize_location(signal.location), _require_normalized(signal)]
    raise ValueError(f"未知的探针：{probe}")


def canonical(signal: Signal, version: int) -> str:
    return "|".join([f"v{version}", signal.probe.value, *_parts(signal)])


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def fingerprint(signal: Signal, version: int) -> str | None:
    if signal.check == REGRESSION_CHECK:
        return None
    group = signal.ctx(PLATFORM_GROUP)
    if group:
        return str(group)
    return _digest(canonical(signal, version))
