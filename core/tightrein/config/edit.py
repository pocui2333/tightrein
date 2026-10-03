"""接入时把用户的回答写回工作区的 project.yaml(redesign/10-onboarding.md 第 4 节)。

不引入保留注释的 YAML 库，只按行修改一个两级的键(如 git.conventions、extensions.deploy-source、checks.commands)：
顶层段不存在时追加到文件末尾；存在且为块写法时替换或插入该子键所在的几行，其余内容(含注释)不变。写之前重新解析
新文本并与预期的数据比较，不一致(例如顶层段是流式写法、子键写法特殊)时不写入并抛出 EditRejected，由调用方提示
用户手动修改。
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from tightrein.store.files import atomic

INDENT = "  "


class EditRejected(ValueError):
    """无法按行安全地修改，需要用户手动编辑。"""


def _block(name: str, value: Any, indent: str) -> list[str]:
    text = yaml.safe_dump({name: value}, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000)
    return [f"{indent}{line}" if line else line for line in text.rstrip("\n").split("\n")]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _significant(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def updated_text(text: str, key: str, value: Any) -> str:
    parts = key.split(".")
    if len(parts) != 2:
        raise EditRejected(f"只支持两级的键：{key}")
    top, sub = parts
    lines = text.split("\n")
    head = next((index for index, line in enumerate(lines) if re.match(rf"^{re.escape(top)}:\s*(#.*)?$", line)), None)
    if head is None:
        if any(re.match(rf"^{re.escape(top)}:", line) for line in lines):
            raise EditRejected(f"{top} 段不是块写法")
        body = text.rstrip("\n")
        return (body + "\n" if body else "") + "\n".join([f"{top}:", *_block(sub, value, INDENT)]) + "\n"
    end = next((index for index in range(head + 1, len(lines))
                if _significant(lines[index]) and _indent(lines[index]) == 0), len(lines))
    start = next((index for index in range(head + 1, end)
                  if re.match(rf"^{INDENT}{re.escape(sub)}:", lines[index])), None)
    new = _block(sub, value, INDENT)
    if start is None:
        insert = end
        while insert > head + 1 and not lines[insert - 1].strip():
            insert -= 1
        return "\n".join(lines[:insert] + new + lines[insert:])
    stop = next((index for index in range(start + 1, end)
                 if _significant(lines[index]) and _indent(lines[index]) <= len(INDENT)), end)
    while stop > start + 1 and not lines[stop - 1].strip():
        stop -= 1
    return "\n".join(lines[:start] + new + lines[stop:])


def set_value(path: Path, key: str, value: Any) -> None:
    text = path.read_text(encoding="utf-8")
    expected = copy.deepcopy(yaml.safe_load(text) or {})
    top, sub = key.split(".", 1)
    section = expected.setdefault(top, {})
    if not isinstance(section, dict):
        raise EditRejected(f"{top} 不是映射")
    section[sub] = value
    new = updated_text(text, key, value)
    if (yaml.safe_load(new) or {}) != expected:
        raise EditRejected(f"按行修改 {key} 后的内容与预期不一致，请手动修改 {path}")
    atomic.write_text(path, new)
