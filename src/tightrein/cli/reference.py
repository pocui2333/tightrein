"""生成参考文档 docs/reference/：命令、配置字段、交接格式、平台方法，全部从程序与数据读出，不手写。

- commands.md：argparse 命令树(cli/main.build_parser)，帮助文字取文案表；
- configuration.md：settings/defaults.json 的每个键、缺省值与所在的控制键；
- handoff.md：包内每个 `*.schema.json`(各步骤的必填事实、agent 的结构化输出、项目脚本的输出)；
- methods.md：包内每个方法清单(`<方法>.yaml`，protocol/methods.py 加载的那种)。

改了命令、缺省值、schema 或方法清单后运行 `python -m tightrein.cli.reference [目标目录]` 重新生成；
缺省写到仓库的 docs/reference/。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tightrein.cli.main import PROG, build_parser
from tightrein.store.files.atomic import write_text
from tightrein.store.files.json import read_json
from tightrein.store.files.layout import ToolLayout

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
LANGUAGE = "zh"
COMMON_DESTS = frozenset({"help", "project", "json", "lang", "yes"})  # 全局选项：只在开头列一次
CONTROLS = "controls"
CONTROLS_DEFAULT = "*"
HELP_TEXT = "显示帮助"  # argparse 自带的 -h 说明是英文
INLINE_LIMIT = 100  # 缺省值的 JSON 超过这么多字符时放进折叠块
SCHEMA_DEPTH = 6  # $ref 展开的层数上限(防止自引用无限展开)
TYPE_NAMES = {"string": "字符串", "integer": "整数", "number": "数字", "boolean": "布尔", "object": "对象",
              "array": "数组", "null": "null"}
SECTION_DOCS: dict[str, str] = {
    "limits": "protocol/limits.md",
    "resources": "protocol/resources.md",
    "boundaries": "protocol/boundaries.md",
    "records": "protocol/records.md",
    "schedule": "protocol/schedule.md",
    "git": "protocol/git.md",
}
GENERATED = "> 本文件由 `src/tightrein/cli/reference.py` 生成，不要手改；改了{source}后运行 `python -m tightrein.cli.reference`。"
CONFIGURATION_INTRO = (
    "取值的读取顺序：`settings/defaults.json` → `settings/controls.json`(本机) → 工作区 `settings.json` 的 `overrides`，"
    "后者覆盖前者。合并规则、`+` 追加、只许追加的列表与不能覆盖的项见 `settings/README.md`；"
    "`tightrein project config <键> --explain` 列出每一层给出的值。"
)
MODELS_INTRO = (
    "模型别名：控制字段 `model`、`modelWhen`、`fallback` 写别名，这里定别名对应的工具、模型、推理强度与价格"
    "(每百万 token 的美元，工具不报费用时估算用)。"
)
CONTROLS_INTRO = (
    "控制键写成「阶段.模块.小步骤」，字段按「小步骤 → 模块 → 阶段 → `*`」继承：下表每一行只列该控制键自己写的键，"
    "没写的取上一级。同一处也放各模块自己的参数(由模块按自己的 schema 校验)。"
)
HANDOFF_INTRO = (
    "每一步的交接都分四部分：结论、必填事实、量化数据、备注，格式与校验见 `src/tightrein/protocol/handoff.md`。"
    "下面是包内每个 schema(JSON Schema 2020-12)的字段：步骤交接的必填事实、agent 的结构化输出、项目脚本的输出。"
)
METHODS_INTRO = (
    "每个方法是一个程序加一个同名清单，放在模块的方法目录下；接入清单 `setup.json` 中该模块的 `method` 写方法名。"
    "参数合并自 `sites.json` 的 `site` 分组、settings 中该来源控制键下以方法名为键的一节，按 `optionsSchema` 校验；"
    "凭据取 `secrets.json` 的 `secret` 条目。加一种方法见 `docs/how-to/add-method.md`。"
)


@dataclass(frozen=True)
class Argument:
    usage: str  # 写进命令行概要的写法，如 `[--doc {pending,failure,deliver}]`
    name: str  # 表格中的名称
    required: bool
    default: str
    help: str


@dataclass(frozen=True)
class Command:
    words: tuple[str, ...]  # ("project", "add")
    help: str
    arguments: tuple[Argument, ...]


@dataclass(frozen=True)
class Field:
    path: str
    type: str
    required: bool
    description: str


@dataclass(frozen=True)
class Reference:
    name: str
    text: str


def generate(tool: ToolLayout) -> list[Reference]:
    parser = build_parser(LANGUAGE)
    return [
        Reference("commands.md", commands(parser)),
        Reference("configuration.md", configuration(read_json(tool.defaults))),
        Reference("handoff.md", handoff(PACKAGE_ROOT)),
        Reference("methods.md", methods(PACKAGE_ROOT)),
    ]


def write(tool: ToolLayout, target: Path) -> list[Path]:
    written = []
    for item in generate(tool):
        path = target / item.name
        write_text(path, item.text)
        written.append(path)
    return written


def commands(parser: argparse.ArgumentParser) -> str:
    walked = list(_commands(parser, ()))
    lines = ["# 命令", "", GENERATED.format(source="命令或帮助文字"), ""]
    lines += ["命令的通用规则(编号写法、确认、`--json` 的输出)与退出码见 `src/tightrein/cli/README.md`。", ""]
    lines += ["## 全局选项", "", "每个命令都接受，写在命令之后：", "", "| 选项 | 说明 |", "|---|---|"]
    for action in _common_actions(parser):
        described = HELP_TEXT if action.dest == "help" else action.help or ""
        lines.append(f"| `{', '.join(action.option_strings)}{_metavar_suffix(action)}` | {_cell(described)} |")
    lines += ["", f"不带命令时执行 `{PROG} status`。", ""]
    groups = list(dict.fromkeys(item.words[0] for item in walked if len(item.words) == 2))
    lines += ["## 一览", "", "| 命令 | 做什么 |", "|---|---|"]
    lines += [f"| `{PROG} {' '.join(item.words)}` | {_cell(item.help)} |" for item in walked]
    lines.append("")
    lines += ["## 日常命令", ""]
    for item in walked:
        if len(item.words) == 1 and item.words[0] not in groups:
            lines += _command_section(item)
    for group in groups:
        head = next(item for item in walked if item.words == (group,))
        lines += [f"## {group}", "", head.help, ""]
        for item in walked:
            if len(item.words) == 2 and item.words[0] == group:
                lines += _command_section(item)
    return "\n".join(lines)


def configuration(defaults: Mapping[str, Any]) -> str:
    lines = ["# 配置字段", "", GENERATED.format(source=" `settings/defaults.json` "), ""]
    lines += [CONFIGURATION_INTRO, "", "下面列出 `defaults.json` 中的每个键与缺省值。", ""]
    for section, value in defaults.items():
        if section == CONTROLS:
            lines += _controls_section(value)
        elif section == "models":
            lines += _models_section(value)
        else:
            lines += _plain_section(section, value)
    return "\n".join(lines)


def handoff(root: Path) -> str:
    lines = ["# 交接格式", "", GENERATED.format(source=" schema "), ""]
    lines += [HANDOFF_INTRO, ""]
    schemas = sorted(root.rglob("*.schema.json"))
    lines += ["| 文件 | 内容 |", "|---|---|"]
    for path in schemas:
        lines.append(f"| [`{_relative(path, root)}`](#{_anchor(_relative(path, root))}) | "
                     f"{_cell(read_json(path).get('title', ''))} |")
    lines.append("")
    for path in schemas:
        schema = read_json(path)
        lines += [f"## {_relative(path, root)}", ""]
        if schema.get("title"):
            lines += [schema["title"], ""]
        if schema.get("description"):
            lines += [schema["description"], ""]
        fields = list(_fields(schema, schema, "", 0))
        if fields:
            lines += ["| 字段 | 类型 | 必填 | 说明 |", "|---|---|---|---|"]
            lines += [f"| `{item.path}` | {_cell(item.type)} | {'是' if item.required else '否'} | "
                      f"{_cell(item.description)} |" for item in fields]
            lines.append("")
    return "\n".join(lines)


def methods(root: Path) -> str:
    lines = ["# 平台方法", "", GENERATED.format(source="方法清单(`<方法>.yaml`)"), ""]
    lines += [METHODS_INTRO, ""]
    manifests = sorted(path for path in root.rglob("*.yaml") if path.with_suffix(".py").is_file())
    lines += ["| 方法 | 目录 | 做什么 |", "|---|---|---|"]
    for path in manifests:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        lines.append(f"| [`{manifest['name']}`](#{_anchor(_relative(path, root))}) | "
                     f"`{_relative(path.parent, root)}/` | {_cell(manifest.get('summary', ''))} |")
    lines.append("")
    for path in manifests:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        lines += [f"## {_relative(path, root)}", "", manifest.get("summary", ""), ""]
        facts = [("适用", manifest.get("applicability")), ("地址", _code(manifest.get("site"), "sites.json 的 ")),
                 ("凭据", _code(manifest.get("secret"), "secrets.json 的 ")),
                 ("凭据必填", _yes_no(manifest.get("secretRequired")) if manifest.get("secret") else None)]
        lines += [f"- {name}：{value}" for name, value in facts if value]
        lines.append("")
        schema = manifest.get("optionsSchema") or {}
        fields = list(_fields(schema, schema, "", 0))
        if fields:
            lines += ["参数：", "", "| 键 | 类型 | 必填 | 说明 |", "|---|---|---|---|"]
            lines += [f"| `{item.path}` | {_cell(item.type)} | {'是' if item.required else '否'} | "
                      f"{_cell(item.description)} |" for item in fields]
            lines.append("")
        if manifest.get("limitations"):
            lines += ["限制：", ""] + [f"- {item}" for item in manifest["limitations"]] + [""]
        if manifest.get("example"):
            lines += ["示例：", "", "```json", manifest["example"].rstrip("\n"), "```", ""]
    return "\n".join(lines)


def _commands(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> Iterator[Command]:
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helps = {choice.dest: choice.help or "" for choice in action._choices_actions}
        for name, sub in action.choices.items():
            words = (*prefix, name)
            yield Command(words, helps.get(name, ""), tuple(_arguments(sub)))
            yield from _commands(sub, words)


def _arguments(parser: argparse.ArgumentParser) -> Iterator[Argument]:
    exclusive = {id(action): group for group in parser._mutually_exclusive_groups for action in group._group_actions}
    for action in parser._actions:
        if (isinstance(action, argparse._SubParsersAction) or action.dest in COMMON_DESTS
                or action.help == argparse.SUPPRESS):
            continue
        name = ", ".join(action.option_strings) if action.option_strings else _metavar(action)
        usage = (f"{action.option_strings[-1]}{_metavar_suffix(action)}" if action.option_strings
                 else _metavar(action))
        required = action.required or (not action.option_strings and action.nargs not in ("?", "*"))
        help_text = action.help or ""
        group = exclusive.get(id(action))
        if group is not None:
            members = group._group_actions
            others = "、".join(f"`{item.option_strings[-1]}`" for item in members if item is not action)
            help_text += f"(与 {others} {'必选其一' if group.required else '二选一'})"
            required = group.required
            # 同一组在概要里只写一次：(a | b) 或 [a | b]，写在组内第一个上
            joined = " | ".join(f"{item.option_strings[-1]}{_metavar_suffix(item)}" for item in members)
            usage = (f"({joined})" if group.required else f"[{joined}]") if action is members[0] else ""
        elif not required:
            usage = f"[{usage}]"
        if action.choices is not None and not action.option_strings and action.metavar is not None:
            help_text = f"{help_text}；取值 {', '.join(map(str, action.choices))}".lstrip("；")
        shown = f"{name}{_metavar_suffix(action)}" if action.option_strings else name
        yield Argument(usage, shown, required, _default(action), help_text or "—")


def _command_section(item: Command) -> list[str]:
    synopsis = " ".join([PROG, *item.words, *(argument.usage for argument in item.arguments if argument.usage)])
    lines = [f"### {' '.join(item.words)}", "", item.help, "", "```", synopsis, "```", ""]
    if item.arguments:
        lines += ["| 参数 | 必填 | 缺省 | 说明 |", "|---|---|---|---|"]
        lines += [f"| `{argument.name}` | {'是' if argument.required else '否'} | {argument.default} | "
                  f"{_cell(argument.help)} |" for argument in item.arguments]
        lines.append("")
    return lines


def _common_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    first = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    sample = next(iter(first.choices.values()))
    return [action for action in sample._actions if action.dest in COMMON_DESTS]


def _metavar(action: argparse.Action) -> str:
    if isinstance(action.metavar, str):
        return action.metavar
    if action.choices is not None:
        return "{" + ",".join(map(str, action.choices)) + "}"
    return f"<{action.dest}>"


def _metavar_suffix(action: argparse.Action) -> str:
    """选项后面跟的值；开关类(store_true、store_const)没有。"""
    if action.nargs == 0:
        return ""
    return f" {_metavar(action)}"


def _default(action: argparse.Action) -> str:
    if action.nargs == 0 or action.default in (None, [], argparse.SUPPRESS):
        return "—"
    return f"`{action.default}`"


def _models_section(models: Mapping[str, Any]) -> list[str]:
    lines = ["## models", "", MODELS_INTRO, ""]
    lines += ["| 别名 | 工具 | 模型 | 推理强度 | 价格(输入/输出/缓存读/缓存写) |", "|---|---|---|---|---|"]
    for alias, model in models.items():
        price = model.get("price")
        priced = "—" if not price else " / ".join(str(price.get(key)) for key in ("input", "output", "cacheRead",
                                                                                    "cacheWrite"))
        lines.append(f"| `{alias}` | {model.get('tool')} | `{model.get('model')}` | {model.get('effort') or '—'} | "
                     f"{priced} |")
    return lines + [""]


def _controls_section(controls: Mapping[str, Any]) -> list[str]:
    lines = ["## controls", "", CONTROLS_INTRO, ""]
    lines += ["### 统一的控制字段(`*`)", "", "| 字段 | 全局缺省 |", "|---|---|"]
    lines += [f"| `{name}` | {_value(value)} |" for name, value in controls[CONTROLS_DEFAULT].items()]
    lines += ["", "### 各控制键", "", "| 控制键 | 键 | 缺省值 |", "|---|---|---|"]
    for key, section in controls.items():
        if key == CONTROLS_DEFAULT:
            continue
        for path, value in _leaves(section, ""):
            lines.append(f"| `{key}` | `{path}` | {_value(value)} |")
    return lines + [""]


def _plain_section(section: str, value: Any) -> list[str]:
    lines = [f"## {section}", ""]
    if section in SECTION_DOCS:
        lines += [f"含义见 `src/tightrein/{SECTION_DOCS[section]}`。", ""]
    if not isinstance(value, Mapping):
        return lines + [_value(value), ""]
    lines += ["| 键 | 缺省值 |", "|---|---|"]
    lines += [f"| `{section}.{path}` | {_value(item)} |" for path, item in _leaves(value, "")]
    return lines + [""]


def _leaves(value: Mapping[str, Any], prefix: str) -> Iterator[tuple[str, Any]]:
    """映射逐层展开到叶子；空映射与列表作为叶子。键里带点的(如控制键)加引号，免得与层级混淆。"""
    for key, item in value.items():
        name = f'"{key}"' if "." in key or "*" in key else key
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(item, Mapping) and item:
            yield from _leaves(item, path)
        else:
            yield path, item


def _value(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= INLINE_LIMIT:
        return f"`{_cell(text)}`"
    return f"<details><summary>{_summary(value)}</summary><code>{_cell(_escape_html(text))}</code></details>"


def _summary(value: Any) -> str:
    if isinstance(value, list):
        return f"{len(value)} 项"
    if isinstance(value, Mapping):
        return f"{len(value)} 个键"
    return "展开"


def _fields(node: Mapping[str, Any], schema: Mapping[str, Any], prefix: str, depth: int) -> Iterator[Field]:
    node = _resolve(node, schema)
    required = set(node.get("required", ()))
    for name, child in (node.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        resolved = _resolve(child, schema)
        yield Field(path, _type(resolved, schema), name in required, _description(child, resolved))
        if depth < SCHEMA_DEPTH:
            yield from _nested(resolved, schema, path, depth + 1)


def _nested(node: Mapping[str, Any], schema: Mapping[str, Any], path: str, depth: int) -> Iterator[Field]:
    if "properties" in node:
        yield from _fields(node, schema, path, depth)
    items = node.get("items")
    if isinstance(items, Mapping):
        yield from _nested(_resolve(items, schema), schema, f"{path}[]", depth)
    for variant in node.get("anyOf", []) + node.get("oneOf", []):
        yield from _nested(_resolve(variant, schema), schema, path, depth)


def _resolve(node: Mapping[str, Any], schema: Mapping[str, Any]) -> Mapping[str, Any]:
    reference = node.get("$ref")
    if not isinstance(reference, str):
        return node
    target: Any = schema
    for part in reference.removeprefix("#/").split("/"):
        target = target[part]  # 只有本文件内的引用(protocol/handoff.load_schema 已校验)
    return {**target, **{key: value for key, value in node.items() if key != "$ref"}}


def _type(node: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    if "const" in node:
        return f"`{json.dumps(node['const'], ensure_ascii=False)}`"
    if "enum" in node:
        return " | ".join(f"`{json.dumps(item, ensure_ascii=False)}`" for item in node["enum"])
    variants = node.get("anyOf") or node.get("oneOf")
    if variants:
        return " 或 ".join(_type(_resolve(item, schema), schema) for item in variants)
    kinds = node.get("type")
    names = [TYPE_NAMES.get(kind, kind) for kind in ([kinds] if isinstance(kinds, str) else list(kinds or []))]
    if isinstance(node.get("items"), Mapping):
        element = _type(_resolve(node["items"], schema), schema)
        names = [f"数组(元素：{element})" if name == TYPE_NAMES["array"] else name for name in names]
    text = " 或 ".join(names) or "任意"
    if node.get("pattern"):
        text += f"(`{node['pattern']}`)"
    return text


def _description(original: Mapping[str, Any], resolved: Mapping[str, Any]) -> str:
    return str(original.get("description") or resolved.get("description") or "—")


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _anchor(heading: str) -> str:
    """GitHub 的标题锚点：小写，去掉标点(保留连字符与下划线)，空格换连字符。"""
    kept = "".join(char for char in heading.lower() if char.isalnum() or char in "-_ ")
    return kept.replace(" ", "-")


def _code(value: Any, prefix: str) -> str | None:
    return f"{prefix}`{value}`" if value else None


def _yes_no(value: Any) -> str:
    return "是" if value else "否"


def _cell(text: str) -> str:
    """放进表格单元格：竖线转义，换行换成空格。"""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m tightrein.cli.reference",
                                     description="由命令树、settings/defaults.json、各步骤 schema 与方法清单生成 docs/reference/")
    parser.add_argument("target", nargs="?", type=Path, help="输出目录，缺省为仓库的 docs/reference/")
    args = parser.parse_args(argv)
    tool = ToolLayout.discover()
    target = args.target or tool.root / "docs" / "reference"
    for path in write(tool, target):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
