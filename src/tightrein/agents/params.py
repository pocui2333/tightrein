"""调用 AI 的统一参数(agents/README.md「统一参数」)。所有模型调用只走 agents.call.call，参数就是这一张表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# 调用点按条件选模型(settings 的 modelWhen)：条件名只能取这里声明过的，各调用点只认自己声明的条件；
# 配置校验据此核对 modelWhen 的写法(settings/load.py)
HIGH_RISK = "high_risk"  # 方案或改动判为高风险(implement/design/risk.py)
FRONTEND = "frontend"  # 根因位置含前端文件(implement/prompts/locate.py)
CONDITIONS: dict[str, tuple[str, ...]] = {
    "implement.locate": (FRONTEND,),
    "implement.design": (HIGH_RISK,),
    "implement.code": (HIGH_RISK,),
    "implement.code.continue": (HIGH_RISK,),
    "implement.review": (HIGH_RISK,),
    "implement.review.deep": (HIGH_RISK,),
}


class Access(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class Model:
    """模型别名解析后的结果：工具、模型、推理强度与价格(每百万 token 的美元，用于工具不报费用时估算)。"""

    alias: str
    tool: str  # claude、agy、codex、replay
    model: str
    effort: str | None = None
    price_input: float | None = None
    price_output: float | None = None
    price_cache_read: float | None = None
    price_cache_write: float | None = None


@dataclass(frozen=True)
class Limits:
    timeout_s: float
    turns: int
    output_tokens: int
    input_tokens: int
    idle_s: float  # 流式输出多久没有动静即超时


@dataclass(frozen=True)
class CallParams:
    # 身份
    point: str  # 调用点 = 控制键：implement.design、assess.triage、retro.idea
    run: str
    subject: str | None
    # 模型
    model: Model
    fallback: Model | None
    # 输入
    prompt: str
    schema: dict[str, Any] | None
    workdir: Path
    read_paths: tuple[Path, ...] = ()
    # 权限
    access: Access = Access.READ
    allowed_commands: tuple[str, ...] = ()
    network: bool = False
    # 上限
    limits: Limits = field(default_factory=lambda: Limits(600, 15, 16_000, 50_000, 300))
    # 会话
    resume_session: str | None = None
    finalize: bool = False  # 结束后续接同一会话补要结构化结果
    # 环境
    language: str = "zh"
    prompt_hash: str | None = None  # 所用提示文件的哈希，记进量化数据
    # 身份(续)：会多轮的步骤的第几轮，进 prompt、raw 等文件名(protocol/naming.md)
    round: int | None = None
