"""由问题的探针数据生成英文简称(architecture/06 10.2)，纯函数。

api-fuzz 取路由模板的末两段(去掉路径参数)加状态码或检查名；内部错误取异常类型与首个本项目帧的方法名；
static 与 incidental 取缺陷模式或检查名与方法名。统一转为小写、以 `-` 连接、截断到 max_length；取不到英文词时为
「探针-问题序号」。简称同时用于修复分支名的描述部分。
"""

from __future__ import annotations

import re

from tightrein.domain.enums import Probe
from tightrein.domain.ids import parse_sequence
from tightrein.domain.problem import Problem
from tightrein.domain.signal import Signal

NOT_WORD = re.compile(r"[^a-z0-9]+")
PARAMETER = re.compile(r"^[{:<].*")


def normalize(text: str, max_length: int) -> str:
    return NOT_WORD.sub("-", text.lower()).strip("-")[:max_length].rstrip("-")


def _method(location: str) -> str:
    symbol = location.rpartition(":")[2] if ":" in location else location
    return symbol.rpartition(".")[2]


def words(problem: Problem, latest: Signal | None) -> list[str]:
    probe = problem.probe
    if probe is Probe.API_FUZZ:
        route = problem.scope.location.partition(" ")[2] or problem.scope.location
        segments = [part for part in route.split("/") if part and not PARAMETER.match(part)]
        status = latest.ctx("response", "status") if latest is not None else None
        return [*segments[-2:], str(status) if status is not None else (latest.check if latest else "")]
    if latest is None:
        return []
    if probe is Probe.PLATFORM_ERRORS:
        frames = [frame for frame in latest.ctx("projectFrames") or [] if isinstance(frame, dict)]
        exception = str(latest.ctx("exceptionType") or "").rpartition(".")[2]
        return [exception, _method(str(frames[0].get("symbol") or "")) if frames else ""]
    return [latest.check, _method(problem.scope.location)]


def slug(problem: Problem, latest: Signal | None, max_length: int) -> str:
    found = normalize("-".join(word for word in words(problem, latest) if word), max_length)
    return found or f"{problem.probe.value}-{parse_sequence(problem.id)}"
