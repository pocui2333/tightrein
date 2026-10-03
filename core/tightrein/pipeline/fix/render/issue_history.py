"""Issue「历史」一节的修复摘要行(architecture/07 4.12)：改了哪些文件、为什么这样改、风险判定与评审深度、检查结果、
遗留事项。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def fix_line(outputs: Mapping[str, Any]) -> str:
    files = "、".join(f"`{item['path']}`" for item in outputs.get("changedFiles") or []) or "无"
    risk = (outputs.get("risk") or {}).get("apply") or {}
    level = "高风险(深度评审)" if risk.get("level") == "high" else "常规(轻量评审)"
    rounds = outputs.get("rounds") or []
    leftovers = "；".join(outputs.get("leftovers") or []) or "无"
    return (f"修复：{outputs.get('summary') or '无'}；改动 {files}；风险 {level}；经过 {len(rounds)} 轮检查与评审；"
            f"遗留：{leftovers}")
