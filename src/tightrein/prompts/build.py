"""提示拼装(prompts/README.md)：模板的固定部分 + 共用片段 + 程序填入的变量，各步骤不再自己写提示文字。

拼出来的顺序固定，前面的部分对同一个调用点每次都一样，用得上提示缓存：

角色 → 要做的事 → 规则与边界(+ common/boundaries.md、common/notes.md) → 输出语言(common/language.md)
→ 输出(+ common/output.md、schema 说明) → 本次输入(唯一的变动部分)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from tightrein.protocol.naming import check_control_key

PROMPTS_DIR = Path(__file__).parent
SECTIONS = ("角色", "要做的事", "规则与边界", "输出", "本次输入")
INPUT_SECTION = "本次输入"
LANGUAGE_NAMES = {"zh": "简体中文", "en": "English", "ja": "日本語"}
# 能按 schema 约束输出的工具(claude、agy 用 --json-schema，codex 用 --output-schema；replay 回放录制)；
# 其余工具看不到 schema 文件，只能在提示里附上展开后的全文
NATIVE_SCHEMA_TOOLS = frozenset({"claude", "agy", "codex", "replay"})
# 模板用到这个变量时才附上「代码笔记的用法」
NOTES_VARIABLE = "notes"
SCHEMA_NOTE = (
    "只输出一个符合输出格式(schema)的 JSON 对象，不要输出其他文字。"
    "格式由调用方提供，不在项目里，不要去找这个文件。"
)

_VARIABLE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LOCAL_REF = ("#/$defs/", "#/definitions/")


class PromptError(Exception):
    """模板不合规、变量缺失或多余、语言不认识；problems 一次列出全部。"""

    def __init__(self, point: str, problems: list[str]) -> None:
        super().__init__(f"{point} 的提示拼不出来：" + "；".join(problems))
        self.point = point
        self.problems = problems


@dataclass(frozen=True)
class Prompt:
    text: str
    hash: str  # 所用模板文件与共用片段的哈希，记进量化数据(Versions.prompt)，改提示前后可对比


@dataclass(frozen=True)
class _Template:
    source: str
    sections: dict[str, str]
    variables: frozenset[str]


def build(
    point: str,
    variables: Mapping[str, str],
    *,
    language: str,
    schema: dict[str, Any] | None,
    tool: str,
    root: Path = PROMPTS_DIR,
) -> Prompt:
    template = _template(root / f"{check_control_key(point)}.md")
    problems = [f"缺少变量 {{{{{name}}}}}" for name in sorted(template.variables - variables.keys())]
    problems += [f"模板中没有变量 {{{{{name}}}}}" for name in sorted(variables.keys() - template.variables)]
    if language not in LANGUAGE_NAMES:
        problems.append(f"不认识的输出语言 {language!r}(可用：{'、'.join(LANGUAGE_NAMES)})")
    if problems:
        raise PromptError(point, problems)

    fragments = ["boundaries", "language", "output"]
    if NOTES_VARIABLE in template.variables:
        fragments.append(NOTES_VARIABLE)
    common = {name: _read(root / "common" / f"{name}.md") for name in fragments}
    rules = [template.sections["规则与边界"], common["boundaries"]]
    if NOTES_VARIABLE in common:
        rules.append(common[NOTES_VARIABLE])
    output = [template.sections["输出"], common["output"]]
    if schema is not None:
        output.append(SCHEMA_NOTE)
        if tool not in NATIVE_SCHEMA_TOOLS:
            output.append(f"```json\n{json.dumps(_expand(schema), ensure_ascii=False, indent=2)}\n```")

    parts = [
        ("角色", template.sections["角色"]),
        ("要做的事", template.sections["要做的事"]),
        ("规则与边界", "\n\n".join(rules)),
        ("输出语言", _fill(common["language"], {"language": LANGUAGE_NAMES[language]})),
        ("输出", "\n\n".join(output)),
        (INPUT_SECTION, _fill(template.sections[INPUT_SECTION], variables)),
    ]
    text = "\n\n".join(f"# {heading}\n\n{body}" for heading, body in parts) + "\n"
    digest = hashlib.sha256(template.source.encode())
    for name in sorted(common):
        digest.update(f"\0{name}\0{common[name]}".encode())
    return Prompt(text=text, hash=digest.hexdigest()[:12])


def check_template(path: Path) -> list[str]:
    """提示文件是否符合 TEMPLATE.md；返回全部问题，合格时为空。"""
    return _problems(path.name, path.read_text(encoding="utf-8"))[0]


@cache
def _template(path: Path) -> _Template:
    if not path.is_file():
        raise PromptError(path.stem, [f"没有提示模板 {path}"])
    source = path.read_text(encoding="utf-8")
    problems, sections = _problems(path.name, source)
    if problems:
        raise PromptError(path.stem, problems)
    names = frozenset(match.group(1) for match in _VARIABLE.finditer(sections[INPUT_SECTION]))
    return _Template(source=source, sections=sections, variables=names)


@cache
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _problems(name: str, source: str) -> tuple[list[str], dict[str, str]]:
    problems: list[str] = []
    stem, suffix = name.rsplit(".", 1) if "." in name else (name, "")
    try:
        if suffix != "md":
            raise ValueError("扩展名要是 .md")
        check_control_key(stem)
    except ValueError as error:
        problems.append(f"文件名要写成「<控制键>.md」：{name}({error})")
    preamble, order, sections = _split(source)
    if preamble:
        problems.append("第一个标题之前不能有内容")
    if order != list(SECTIONS):
        problems.append(f"一级标题要依次为 {'、'.join(SECTIONS)}，实际为 {'、'.join(order) or '无'}")
    for heading in SECTIONS:
        if heading in sections and not sections[heading]:
            problems.append(f"「{heading}」是空的")
    for heading, body in sections.items():
        found = [match.group(1) for match in _VARIABLE.finditer(body)]
        if heading != INPUT_SECTION and found:
            problems.append(f"「{heading}」是固定部分，不能有变量：{'、'.join(found)}")
        problems += [f"变量名要用小写英文与下划线：{{{{{item}}}}}" for item in found if not _VARIABLE_NAME.match(item)]
    if INPUT_SECTION in sections and not _VARIABLE.search(sections[INPUT_SECTION]):
        problems.append(f"「{INPUT_SECTION}」中没有变量")
    return problems, sections


def _split(source: str) -> tuple[str, list[str], dict[str, str]]:
    """按一级标题切分；代码块中以 `# ` 开头的行(如 shell 注释)不算标题。"""
    preamble: list[str] = []
    order: list[str] = []
    bodies: dict[str, list[str]] = {}
    current = preamble
    fence: str | None = None
    for line in source.splitlines():
        marker = _FENCE.match(line)
        if marker is not None:
            token = marker.group(1)
            # CommonMark：同种字符且不短于开启标记才算闭合
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None and line.startswith("# "):
            heading = line[2:].strip()
            order.append(heading)
            current = bodies.setdefault(heading, [])
            continue
        current.append(line)
    return "\n".join(preamble).strip(), order, {key: "\n".join(lines).strip() for key, lines in bodies.items()}


def _fill(text: str, values: Mapping[str, str]) -> str:
    # 一次替换：变量的值里即使出现 {{…}} 也不会再被展开
    return _VARIABLE.sub(lambda match: values[match.group(1)], text)


def _expand(schema: dict[str, Any]) -> dict[str, Any]:
    """把本文件内的 `$ref` 展开成全文；递归引用保留 `$ref` 与定义，避免无限展开。"""
    definitions = {**schema.get("definitions", {}), **schema.get("$defs", {})}

    def resolve(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith(_LOCAL_REF):
            target = reference.rsplit("/", 1)[1]
            if target in definitions and target not in seen:
                rest = {key: value for key, value in node.items() if key != "$ref"}
                return {**resolve(definitions[target], seen | {target}), **resolve(rest, seen)}
        return {key: resolve(value, seen) for key, value in node.items() if key not in ("$defs", "definitions")}

    expanded = resolve(schema, frozenset())
    if any(marker in json.dumps(expanded) for marker in _LOCAL_REF):
        expanded.update({key: schema[key] for key in ("definitions", "$defs") if key in schema})
    return expanded


__all__ = ["LANGUAGE_NAMES", "Prompt", "PromptError", "build", "check_template"]
