"""从 JSON schema 生成契约参考的字段表(redesign/00-documentation.md「契约参考」)。

用法(在 core 目录)：
    .venv/bin/python dev/contract_reference.py handoff|probe|methods [--check]
写出 docs/reference/ 下对应的文件；--check 只比较，文件与生成结果不同时以退出码 1 结束。
字段表的列为名称、类型、必填、说明、示例：说明与示例取 schema 的 description 与 examples，嵌套对象与数组元素展开为
点号路径(数组元素写作 `[]`)。新的契约参考在 TARGETS 中登记一个生成函数。
"""

from __future__ import annotations

import argparse
import json
import posixpath
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tightrein.config.layers import core_defaults
from tightrein.contracts import validate
from tightrein.domain.enums import ExtensionPoint
from tightrein.domain.handoff import types
from tightrein.extensions import catalog

CORE = Path(__file__).resolve().parents[1]
REFERENCE_DIR = CORE.parent / "docs" / "reference"
GENERATED = "<!-- 本文件由 core/dev/contract_reference.py 生成，不要手改；改 schema 或类型注册后重新生成。 -->"
LANGUAGES = ("zh", "en", "ja")
PROBE_INPUT = "data/project-probe-input.schema.json"
PROBE_OUTPUT = "data/project-probe-output.schema.json"
MISSING = object()
PLANNED = ("以下平台留作后续，按方法目录的写法新增方法即可，不改核心：错误追踪 Rollbar、Bugsnag、Honeybadger；"
           "集中日志 Elasticsearch/OpenSearch、云厂商的日志服务(CloudWatch Logs、阿里云 SLS 等)、Datadog Logs；"
           "业务告警 Grafana 告警的 Ruler API、云监控的告警接口。")


def _resolve(node: dict[str, Any], name: str) -> tuple[dict[str, Any], str]:
    """展开 node 的 $ref(可以指向其他 schema 文件)，返回目标与目标所在的 schema 名称；node 上的其他键覆盖目标。"""
    while "$ref" in node:
        path, _, pointer = node["$ref"].partition("#")
        target_name = posixpath.normpath(posixpath.join(posixpath.dirname(name), path)) if path else name
        target: Any = validate.schema(target_name)
        for part in filter(None, pointer.split("/")):
            target = target[part]
        node = {**target, **{key: value for key, value in node.items() if key != "$ref"}}
        name = target_name
    return node, name


def _type_text(node: dict[str, Any], name: str = "") -> str:
    if "anyOf" in node:
        return " 或 ".join(_type_text(*_resolve(option, name)) for option in node["anyOf"])
    if "const" in node:
        return f"`{json.dumps(node['const'], ensure_ascii=False)}`"
    if "enum" in node:
        return " \\| ".join(f"`{value}`" for value in node["enum"])
    kinds = node.get("type")
    names = {"string": "字符串", "integer": "整数", "number": "数", "boolean": "布尔", "object": "对象",
             "array": "数组", "null": "null"}
    if isinstance(kinds, list):
        return " 或 ".join(names.get(kind, kind) for kind in kinds)
    if kinds is not None:
        text = names.get(kinds, kinds)
        if "pattern" in node:
            text += f"(`{node['pattern']}`)"
        return text
    return "任意"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def fields(node: dict[str, Any], name: str, prefix: str = "") -> list[tuple[str, str, str, str, str]]:
    """一个对象 schema 的字段行：(名称、类型、必填、说明、示例)。"""
    node, name = _resolve(node, name)
    if node.get("type") == "array" and "items" in node:
        return fields(node["items"], name, f"{prefix}[]")
    rows = []
    required = set(node.get("required", ()))
    for key, child in node.get("properties", {}).items():
        resolved, child_name = _resolve(child, name)
        path = f"{prefix}.{key}" if prefix else key
        example = resolved.get("examples")
        rows.append((f"`{path}`", _type_text(resolved, child_name), "是" if key in required else "否",
                     _cell(resolved.get("description", "")),
                     f"`{json.dumps(example[0], ensure_ascii=False)}`" if example else ""))
        if resolved.get("type") == "object" or (resolved.get("type") == "array" and "items" in resolved):
            rows += fields(resolved, child_name, path)
    return rows


def table(rows: list[tuple[str, str, str, str, str]]) -> str:
    lines = ["| 名称 | 类型 | 必填 | 说明 | 示例 |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def handoff() -> str:
    header = validate.schema("handoff/document.schema.json")
    parts = [
        "# 交接文档", "", GENERATED, "",
        "格式与规则见 [交接文档](../explanation/redesign/00-handoff-documents.md)；新增类型见"
        "[如何新增一种交接文档类型](../how-to/add-handoff-document-type.md)。校验：`tightrein doc check <文件>`。", "",
        "## 头信息", "", header["description"], "", table(fields(header, "handoff/document.schema.json")), "",
        "## 基础小节", "", "正文按以下顺序使用二级标题；标题按 `project.language` 写入，读取时三种语言都识别。", "",
        "| 键 | " + " | ".join(LANGUAGES) + " |", "|---|---|---|---|",
        *(f"| `{key}` | " + " | ".join(types.HEADINGS[key][code] for code in LANGUAGES) + " |"
          for key in types.BASE_SECTIONS), "",
        "## 文档类型", "",
        "「内容」下按顺序使用三级标题，全部必需。数据块是信息串为 `yaml data:<标签>` 的代码块，放在所属小节的末尾。", "",
    ]
    for doc in types.TYPES.values():
        schema = validate.schema(doc.schema)
        parts += [f"### {doc.kind}", "", f"{doc.purpose}。数据块的 schema：`{doc.schema}`。", "",
                  "| 小节键 | " + " | ".join(LANGUAGES) + " | 数据块 |", "|---|---|---|---|---|"]
        for key in doc.sections:
            labels = [f"`{label}`{'(必需)' if label in doc.required_blocks else ''}"
                      for label, section in doc.blocks.items() if section == key]
            parts.append(f"| `{key}` | " + " | ".join(types.HEADINGS[key][code] for code in LANGUAGES)
                         + f" | {'、'.join(labels)} |")
        parts.append("")
        for label in doc.blocks:
            block = schema["$defs"][label]
            parts += [f"#### 数据块 `{label}`", "", _cell(block.get("description", "")), "",
                      f"类型：{_type_text(block)}", "", table(fields(block, doc.schema)), ""]
    return "\n".join(parts).rstrip("\n") + "\n"


def probe() -> str:
    parts = ["# 项目探针契约", "", GENERATED, "",
             "写法见[如何编写项目探针](../how-to/write-project-probe.md)。探针经标准输入收到输入 JSON，经标准输出返回"
             "输出 JSON；输出不合格时本次作废、状态不保存。试跑：`tightrein probe test <名称>`。", ""]
    for title, name in (("输入", PROBE_INPUT), ("输出", PROBE_OUTPUT)):
        schema = validate.schema(name)
        parts += [f"## {title}", "", schema["description"], "", f"schema：`{name}`", "", table(fields(schema, name)), ""]
    return "\n".join(parts).rstrip("\n") + "\n"


def _option_rows(method: catalog.Method, defaults: dict[str, Any]) -> list[str]:
    schema = method.options_schema
    required = set(schema.get("required", ()))
    lines = ["| 参数 | 类型 | 必填 | 缺省值 | 说明 |", "|---|---|---|---|---|"]
    for key, node in schema.get("properties", {}).items():
        default = method.options.get(key, defaults.get(key, MISSING))
        shown = "" if default is MISSING else f"`{json.dumps(default, ensure_ascii=False)}`"
        must = "是" if key in required and default is MISSING else "否"
        description = _cell(node.get("description", ""))
        lines.append(f"| `{key}` | {_type_text(node)} | {must} | {_cell(shown)} | {description} |")
    return lines


def methods() -> str:
    defaults = core_defaults().get("methods", {})
    parts = ["# 方法目录", "", GENERATED, "",
             "各扩展点可选用的核心方法(architecture/10 1.4)。项目在 project.yaml 的 `extensions.<扩展点>.use` 中选用，"
             "`options` 填写参数；缺省值来自 config/defaults.yaml 的 `methods.<方法编号>`。每种方法默认不启用，配置了才启用。", ""]
    for point in ExtensionPoint:
        found = [method for method in catalog.core_methods() if method.point is point]
        if not found:
            continue
        parts += [f"## {point.value}({point.label})", ""]
        for method in found:
            doc = method.doc or {}
            parts += [f"### {method.id}", "", method.summary, "", f"**适用条件**：{method.applicability}", ""]
            if doc.get("prerequisites"):
                parts += ["**前提**：", "", *(f"- {item}" for item in doc["prerequisites"]), ""]
            parts += ["**参数**：", "", *_option_rows(method, defaults.get(method.id, {})), ""]
            if doc.get("output"):
                parts += [f"**输出**：{doc['output']}", ""]
            if doc.get("limitations"):
                parts += ["**限制**：", "", *(f"- {item}" for item in doc["limitations"]), ""]
            if doc.get("example"):
                parts += ["**示例配置**：", "", "```yaml", doc["example"].rstrip("\n"), "```", ""]
    parts += ["## 后续平台", "", PLANNED, ""]
    return "\n".join(parts).rstrip("\n") + "\n"


TARGETS: dict[str, tuple[str, Callable[[], str]]] = {
    "handoff": ("handoff-documents.md", handoff),
    "probe": ("project-probe.md", probe),
    "methods": ("methods.md", methods),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 JSON schema 生成契约参考")
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument("--check", action="store_true", help="只比较，不写入")
    args = parser.parse_args(argv)
    file_name, generate = TARGETS[args.target]
    path = REFERENCE_DIR / file_name
    text = generate()
    if args.check:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            print(f"{path} 已过期，执行 .venv/bin/python dev/contract_reference.py {args.target} 重新生成")
            return 1
        print(f"{path} 与 schema 一致")
        return 0
    path.write_text(text, encoding="utf-8")
    print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
