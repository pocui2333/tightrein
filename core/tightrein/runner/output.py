"""结构化结果的提取、校验与重试说明(architecture/02 2.5、2.10 第 9、10 步)。

提取按顺序尝试：整段文本、最后一个标为 json 的代码块、最后一个括号配平的顶层对象；取第一个能解析的 JSON 交给 schema 校验。
三种都失败按校验失败处理。校验不通过时生成重试说明：原输出与逐条错误的 JSON 路径与原因。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.contracts import validate
from tightrein.contracts.validate import FieldError

CODE_FENCE = "`" * 3
CODE_BLOCK = re.compile(r"`{3}json[ \t]*\n(.*?)`{3}", re.DOTALL)
NO_JSON = FieldError("$", "输出中没有可解析的 JSON 对象")


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _last_balanced_object(text: str) -> str | None:
    """最后一个括号配平的顶层 `{...}`；字符串中的括号与转义字符不计入。"""
    candidates: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"' and depth > 0:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                candidates.append(text[start:index + 1])
    return candidates[-1] if candidates else None


def extract_json(text: str | None) -> Any:
    """取第一个能解析的 JSON；都不能解析时返回 None。"""
    if not text or not text.strip():
        return None
    whole = _parse(text.strip())
    if whole is not None:
        return whole
    blocks = CODE_BLOCK.findall(text)
    if blocks:
        value = _parse(blocks[-1].strip())
        if value is not None:
            return value
    balanced = _last_balanced_object(text)
    return None if balanced is None else _parse(balanced)


@dataclass(frozen=True)
class Checked:
    value: Any
    errors: tuple[FieldError, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def check_output(schema: str, structured: Any, final_text: str | None) -> Checked:
    """工具原生的结构化结果优先，否则从最终文本中提取；按 schema 校验。"""
    value = structured if structured is not None else extract_json(final_text)
    if value is None:
        return Checked(None, (NO_JSON,))
    if not isinstance(value, dict):
        return Checked(value, (FieldError("$", "结果须为 JSON 对象"),))
    return Checked(value, tuple(validate.validate(schema, value)))


def retry_note(schema: str, raw: str | None, errors: Sequence[FieldError], limit: int) -> str:
    """第二次尝试附带的说明：哪些路径不合格、原因是什么，以及上一次的原输出(最多 limit 个字符，
    runtime.runner.retryOutputChars)。"""
    lines = [f"上一次的输出不符合 {schema}，请只输出一个符合该 schema 的 JSON 对象。逐条错误："]
    lines += [f"- {error.path}: {error.reason}" for error in errors]
    shown = (raw or "")[:limit]
    lines += ["上一次的原输出：", shown if shown else "(空)"]
    return "\n".join(lines)
