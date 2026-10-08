"""status 与 watch 共用的文字：调用点与来源的显示名、时长与数量、时刻、相对时间。

给人读的文字都取自文案表(cli/text)；单位与数量的写法两种语言相同(protocol/naming.md「中文与英文」)：
`42m`、`1h18m`、`3d`、`1.2M`、`120k`。
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from tightrein.cli.text import text
from tightrein.protocol.naming import format_count, format_duration

TOOL_NAMES = {"claude": "Claude"}  # 标识保持英文原样，只把产品名首字母大写；其余工具名照写
NONE = "—"


def t(language: str, key: str, **values: object) -> str:
    return text(language, key, **values)


def step_name(language: str, point: str) -> str:
    """小步骤的短名：zh「编码」，en「code」；来源：zh「平台错误」，en「platform_errors」。没登记的用最后一段。"""
    return _point(language, point) or point.rsplit(".", 1)[-1]


def point_name(language: str, point: str | None, round_: int | None = None) -> str:
    """调用点全名：zh「实施·编码 r2」，en「implement.code r2」(英文版用控制键原名)。没登记的照写控制键。"""
    if not point:
        return NONE
    stage, step = _point(language, point.split(".", 1)[0]), _point(language, point)
    if "." in point and stage and step:
        name = text(language, "common.point", point=point, stage=stage, step=step)
    elif step:
        name = text(language, "common.point_single", point=point, name=step)
    else:
        name = point
    return f"{name} r{round_}" if round_ else name


def tool_name(tool: str) -> str:
    return TOOL_NAMES.get(tool, tool)


def duration(seconds: float | None) -> str:
    return NONE if seconds is None else format_duration(max(seconds, 0.0))


def count(value: float | None) -> str:
    return NONE if value is None else format_count(int(value))


def percent(ratio: float | None) -> str:
    return NONE if ratio is None else f"{ratio:.0%}"


def size(value: int) -> str:
    """磁盘占用：`820k`、`1.4G`(与 token 数同一种写法，单位换成字节的千进位)。"""
    for unit, scale in (("G", 1 << 30), ("M", 1 << 20), ("k", 1 << 10)):
        if value >= scale:
            return f"{value / scale:.1f}{unit}".replace(f".0{unit}", unit)
    return f"{value}B"


def clock(moment: datetime | None, zone: tzinfo | None, *, seconds: bool = False) -> str:
    if moment is None:
        return NONE
    return moment.astimezone(zone).strftime("%H:%M:%S" if seconds else "%H:%M")


def ago(language: str, now: datetime, moment: datetime | None) -> str:
    if moment is None:
        return NONE
    return text(language, "common.ago", value=duration((now - moment).total_seconds()))


def later(language: str, now: datetime, moment: datetime | None) -> str:
    if moment is None:
        return NONE
    return text(language, "common.later", value=duration((moment - now).total_seconds()))


def left(language: str, now: datetime, moment: datetime | None) -> str:
    if moment is None:
        return NONE
    return text(language, "common.left", value=duration((moment - now).total_seconds()))


def _point(language: str, point: str) -> str | None:
    """文案表 points 区块中登记的名字；调用点不全在表里(各模块可加新的小步骤)，没有时为 None。"""
    try:
        return text(language, f"points.{point}")
    except KeyError:
        return None
