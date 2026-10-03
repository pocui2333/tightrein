"""JSON Schema 2020-12 校验(architecture/01 第 3 节)：按名称加载 contracts/schemas 下的 schema，返回逐条错误的 JSON 路径与原因。

schema 的名称是它相对 schemas 目录的路径，例如 `runner/roles/claim-verifier.schema.json`，与执行器任务的 outputSchema 相同。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as MetaSchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_URI = "https://tightrein.local/schemas/"
SUFFIX = ".schema.json"
SCHEMA_DIR = Path(__file__).parent / "schemas"


class SchemaDefinitionError(Exception):
    """schema 文件本身不合格：无法解析、不符合 2020-12 元 schema、`$id` 与路径不符或引用无法解析。"""


class SchemaNotFound(KeyError):
    """没有这个名称的 schema，或 schema 中没有这个定义。"""


@dataclass(frozen=True, order=True)
class FieldError:
    """一条校验错误：JSON 路径(例如 `$.outputs.verdict`)与原因。"""

    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


class SchemaValidationError(Exception):
    def __init__(self, name: str, errors: list[FieldError]) -> None:
        self.name = name
        self.errors = errors
        lines = "\n".join(str(error) for error in errors)
        super().__init__(f"不符合 {name}：\n{lines}")


class SchemaStore:
    """一个 schemas 目录中的全部 schema；加载时检查每个文件符合 2020-12 元 schema。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._schemas = self._load(root)
        self._registry: Registry = Registry().with_resources(
            (BASE_URI + name, Resource(contents, DRAFT202012)) for name, contents in self._schemas.items()
        )
        self._validators: dict[tuple[str, str | None], Draft202012Validator] = {}

    @staticmethod
    def _load(root: Path) -> dict[str, dict[str, Any]]:
        if not root.is_dir():
            raise SchemaDefinitionError(f"schema 目录不存在：{root}")
        schemas: dict[str, dict[str, Any]] = {}
        for path in sorted(root.rglob(f"*{SUFFIX}")):
            name = path.relative_to(root).as_posix()
            try:
                contents = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise SchemaDefinitionError(f"{name} 不是合法的 JSON：{error}") from error
            if contents.get("$schema") != DIALECT:
                raise SchemaDefinitionError(f"{name} 的 $schema 必须为 {DIALECT}")
            if contents.get("$id") != BASE_URI + name:
                raise SchemaDefinitionError(f"{name} 的 $id 必须为 {BASE_URI + name}")
            try:
                Draft202012Validator.check_schema(contents)
            except MetaSchemaError as error:
                raise SchemaDefinitionError(f"{name} 不符合 2020-12 元 schema：{error.message}") from error
            schemas[name] = contents
        return schemas

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._schemas))

    def schema(self, name: str) -> dict[str, Any]:
        return copy.deepcopy(self._require(name))

    def _require(self, name: str) -> dict[str, Any]:
        if name not in self._schemas:
            raise SchemaNotFound(f"没有名为 {name} 的 schema")
        return self._schemas[name]

    def _validator(self, name: str, definition: str | None) -> Draft202012Validator:
        key = (name, definition)
        if key not in self._validators:
            contents = self._require(name)
            if definition is None:
                target: dict[str, Any] = contents
            elif definition in contents.get("$defs", {}):
                target = {"$ref": f"{BASE_URI}{name}#/$defs/{definition}"}
            else:
                raise SchemaNotFound(f"{name} 中没有定义 {definition}")
            self._validators[key] = Draft202012Validator(target, registry=self._registry)
        return self._validators[key]

    def validate(self, name: str, instance: Any, definition: str | None = None) -> list[FieldError]:
        """返回全部错误，按路径排序；没有错误时返回空列表。definition 指定时按 `$defs` 中的该项校验。"""
        validator = self._validator(name, definition)
        try:
            errors = {FieldError(error.json_path, error.message) for error in validator.iter_errors(instance)}
        except Unresolvable as error:
            raise SchemaDefinitionError(f"{name} 中的引用无法解析：{error}") from error
        return sorted(errors)

    def check(self, name: str, instance: Any, definition: str | None = None) -> None:
        errors = self.validate(name, instance, definition)
        if errors:
            raise SchemaValidationError(name, errors)

    def inline(self, name: str) -> dict[str, Any]:
        """展开全部 `$ref`，得到不依赖其他文件的 schema，供只接受单个 schema 文本的工具使用。"""
        resolver = self._registry.resolver(base_uri=BASE_URI + name)
        result = _inline(self._require(name), resolver, ())
        result.pop("$id", None)
        return result


def _inline(node: Any, resolver: Any, trail: tuple[int, ...]) -> Any:
    if isinstance(node, list):
        return [_inline(item, resolver, trail) for item in node]
    if not isinstance(node, dict):
        return node
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("$ref", "$defs"):
            continue
        result[key] = _inline(value, resolver, trail)
    if "$ref" not in node:
        return result
    try:
        resolved = resolver.lookup(node["$ref"])
    except Unresolvable as error:
        raise SchemaDefinitionError(f"引用无法解析：{node['$ref']}") from error
    if id(resolved.contents) in trail:
        raise SchemaDefinitionError(f"循环引用：{node['$ref']}")
    target = _inline(resolved.contents, resolved.resolver, (*trail, id(resolved.contents)))
    for key in ("$schema", "$id", "title"):
        target.pop(key, None)
    siblings = {key: value for key, value in result.items() if key not in ("$schema", "$id", "title")}
    if not siblings:
        return {**{key: result[key] for key in ("$schema", "title") if key in result}, **target}
    return {**result, "allOf": [*result.get("allOf", []), target]}


@lru_cache(maxsize=1)
def default_store() -> SchemaStore:
    return SchemaStore(SCHEMA_DIR)


def names() -> tuple[str, ...]:
    return default_store().names()


def schema(name: str) -> dict[str, Any]:
    return default_store().schema(name)


def validate(name: str, instance: Any, definition: str | None = None) -> list[FieldError]:
    return default_store().validate(name, instance, definition)


def check(name: str, instance: Any, definition: str | None = None) -> None:
    default_store().check(name, instance, definition)


def inline(name: str) -> dict[str, Any]:
    return default_store().inline(name)


def validate_handoff(document: Any) -> list[FieldError]:
    """交接文档先按统一外层校验，再按该环节的 outputs schema 校验 outputs；status 为 failed 时只校验外层。"""
    errors = validate("handoff/envelope.schema.json", document)
    if errors or document["status"] == "failed":
        return errors
    outputs = validate(f"handoff/outputs/{document['stage']}.schema.json", document["outputs"])
    return [FieldError("$.outputs" + error.path[1:], error.reason) for error in outputs]
