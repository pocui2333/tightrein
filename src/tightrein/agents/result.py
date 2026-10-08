"""调用 AI 的统一结果(agents/README.md「统一结果」)。

所有失败都以结果返回，不向上抛；只有编程或配置错误(未知工具、没有路由)才抛异常。
状态只有固定几种，协议层(protocol/limits.py)按状态决定重试、换备用模型还是停下。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from tightrein.protocol.handoff import Tokens


class CallStatus(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    TURN_LIMIT = "turn_limit"
    BUDGET_LIMIT = "budget_limit"  # 费用或 token 到限
    REFUSED = "refused"  # 被拒绝(安全分类等)
    SCHEMA_INVALID = "schema_invalid"  # 格式不符
    TRANSIENT = "transient"  # 临时错误：限流、5xx、网络
    AUTH_FAILED = "auth_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"  # 订阅额度用完
    UNAVAILABLE = "unavailable"  # 工具不存在或无法启动
    BOUNDARY = "boundary"  # 越界(只读步骤改了文件、碰了禁改文件)
    FAILED = "failed"  # 其余工具失败


@dataclass(frozen=True)
class RateLimit:
    """从工具输出读到的订阅额度信号(claude 的 rate_limit_event、额度用完的报错)。"""

    tool: str
    window: str  # five_hour、weekly、opus、sonnet
    status: str  # allowed、warning、rejected
    used_ratio: float | None
    resets_at: str | None  # ISO UTC


@dataclass
class CallResult:
    status: CallStatus
    tool: str
    model: str
    output: dict[str, Any] | None = None
    text: str | None = None
    error: str | None = None
    tokens: Tokens = field(default_factory=Tokens)
    cost_usd: float | None = None
    cost_estimated: bool = False
    duration_ms: int = 0
    turns: int = 0
    attempts: int = 1
    retries: int = 0
    session_id: str | None = None
    rate_limits: list[RateLimit] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    raw_path: Path | None = None  # 原始输出，只在失败或调试时保存

    @property
    def ok(self) -> bool:
        return self.status is CallStatus.OK
