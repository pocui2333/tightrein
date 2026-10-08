"""交接文档(protocol/handoff.md)：每一步交出的东西按「结论 → 必填事实 → 量化数据 → 备注」四部分。

- 每一步固定落盘一份 `handoff.json`(检查点)，由程序写；agent 只填必填事实中只有它知道的部分与备注；
- 下一步只拿前三部分，备注只给人看；
- 必填事实按那一步的 schema(放在那一步的文件夹)校验，缺了就要求重写；
- 不适用的字段写 null，不省略。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from tightrein.store.files.json import read_json, write_json

NOTES_LIMIT = 4000
VERBATIM_KEYS = frozenset({"facts", "produced"})  # 键名由各步骤自定，往返时原样保留


class Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"  # 待决定：到人工关卡


@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, other: Tokens) -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write

    def weighted(self, cache_read_weight: float) -> int:
        """计入每个 Issue 用量上限的 token：缓存读取按权重(缺省 1/10)计。

        input 已含缓存写入与缓存读取(agents/tools 统一的口径)，这里只把其中的缓存读取换成按权重计，不再另加缓存写入。
        """
        return int(self.input - self.cache_read + self.output + self.cache_read * cache_read_weight)


@dataclass
class Metrics:
    """所有步骤共用的量化数据，全部由程序统计；不适用的为 None。"""

    duration_ms: int | None = None
    calls: int | None = None
    retries: int | None = None
    rounds: int | None = None
    tokens: Tokens | None = None
    cost_usd: float | None = None
    cost_estimated: bool | None = None
    files_read: int | None = None
    lines_read: int | None = None
    files_changed: int | None = None
    lines_changed: int | None = None
    produced: dict[str, int] | None = None
    passed: int | None = None
    failed: int | None = None


@dataclass
class Versions:
    """产生这份交接时的版本：tightrein 的 commit、提示与 settings 的哈希、工具与模型。"""

    tightrein: str | None = None
    prompt: str | None = None
    settings: str | None = None
    tool: str | None = None
    tool_version: str | None = None
    model: str | None = None


@dataclass
class Handoff:
    point: str  # 控制键：implement.design
    subject: str  # 对象编号：0018、P-0003、R-…
    run: str
    status: Status
    summary: str  # 一句话结论
    facts: dict[str, Any]
    metrics: Metrics = field(default_factory=Metrics)
    notes: str | None = None
    round: int | None = None
    created_at: str | None = None
    versions: Versions = field(default_factory=Versions)

    def for_next_step(self) -> dict[str, Any]:
        """交给下一步(含下一次模型调用)的只有前三部分。"""
        return {"status": self.status.value, "summary": self.summary, "facts": self.facts}

    def to_json(self) -> dict[str, Any]:
        return _camel(asdict(self))

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Handoff:
        plain = _snake(data)
        metrics = plain.get("metrics") or {}
        tokens = metrics.get("tokens")
        return cls(
            point=plain["point"],
            subject=plain["subject"],
            run=plain["run"],
            status=Status(plain["status"]),
            summary=plain["summary"],
            facts=plain["facts"],
            metrics=Metrics(**{**metrics, "tokens": Tokens(**tokens) if tokens else None}),
            notes=plain.get("notes"),
            round=plain.get("round"),
            created_at=plain.get("created_at"),
            versions=Versions(**(plain.get("versions") or {})),
        )


class FactsInvalid(Exception):
    """必填事实不合 schema；errors 每条带 JSON 路径与原因，一次列出全部。"""

    def __init__(self, point: str, errors: list[str]) -> None:
        super().__init__(f"{point} 的必填事实不合格式：" + "；".join(errors))
        self.point = point
        self.errors = errors


def check_facts(point: str, facts: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = schema_errors(facts, schema)
    if errors:
        raise FactsInvalid(point, errors)


def schema_errors(value: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    return [_describe(error) for error in sorted(validator.iter_errors(value), key=lambda e: [str(part) for part in e.absolute_path])]


def write(path: Path, handoff: Handoff) -> None:
    if handoff.notes is not None and len(handoff.notes) > NOTES_LIMIT:
        handoff = replace(handoff, notes=handoff.notes[:NOTES_LIMIT] + "…(备注已截断)")
    write_json(path, handoff.to_json())


def read(path: Path) -> Handoff:
    return Handoff.from_json(read_json(path))


class SchemaDefinitionError(Exception):
    """schema 文件本身不合格：元 schema、`$id` 与文件名不一致、引用解析不了。"""


def load_schema(path: Path) -> dict[str, Any]:
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    identifier = schema.get("$id")
    if identifier is not None and not str(identifier).endswith(path.name):
        raise SchemaDefinitionError(f"{path}：$id {identifier} 与文件名不一致")
    for reference in _references(schema):
        if not reference.startswith("#") or _pointer(schema, reference[1:]) is None:
            raise SchemaDefinitionError(f"{path}：引用解析不了：{reference}(只允许本文件内的 #/… 引用)")
    return schema


def _references(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return found + [ref for value in node.values() for ref in _references(value)]
    if isinstance(node, list):
        return [ref for item in node for ref in _references(item)]
    return []


def _pointer(schema: dict[str, Any], pointer: str) -> Any:
    node: Any = schema
    for part in [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/") if p]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _describe(error: ValidationError) -> str:
    location = "/" + "/".join(str(part) for part in error.absolute_path)
    return f"{location}：{error.message}"


def _camel(value: Any) -> Any:
    if isinstance(value, dict):
        return {_to_camel(key): (item if key in VERBATIM_KEYS else _camel(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_camel(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def _snake(value: Any) -> Any:
    if isinstance(value, dict):
        return {_to_snake(key): (item if key in VERBATIM_KEYS else _snake(item)) for key, item in value.items()}
    return value


def _to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _to_snake(name: str) -> str:
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in name)


__all__ = [
    "FactsInvalid",
    "Handoff",
    "Metrics",
    "SchemaDefinitionError",
    "Status",
    "Tokens",
    "Versions",
    "check_facts",
    "load_schema",
    "read",
    "schema_errors",
    "write",
]
