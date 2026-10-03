import json

import pytest
from jsonschema import Draft202012Validator

from tightrein.contracts.validate import (
    BASE_URI,
    DIALECT,
    FieldError,
    SchemaDefinitionError,
    SchemaNotFound,
    SchemaStore,
    SchemaValidationError,
)


def write(root, name, body):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"$schema": DIALECT, "$id": BASE_URI + name, **body}), encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    write(tmp_path, "base.schema.json", {"$defs": {
        "code": {"type": "string", "pattern": "^[A-Z]+$"},
        "item": {"type": "object", "required": ["code"], "properties": {"code": {"$ref": "#/$defs/code"}}},
    }})
    write(tmp_path, "sub/order.schema.json", {
        "type": "object",
        "required": ["id", "items"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "integer"},
            "items": {"type": "array", "items": {"$ref": "../base.schema.json#/$defs/item"}},
            "note": {"$ref": "../base.schema.json#/$defs/code", "maxLength": 3},
        },
    })
    return SchemaStore(tmp_path)


def test_names_are_paths_relative_to_root(store):
    assert store.names() == ("base.schema.json", "sub/order.schema.json")


def test_valid_instance_has_no_errors(store):
    assert store.validate("sub/order.schema.json", {"id": 1, "items": [{"code": "AB"}]}) == []


def test_errors_carry_json_path_and_reason(store):
    errors = store.validate("sub/order.schema.json", {"id": "x", "items": [{"code": "ab"}, {}], "extra": 1})
    assert [error.path for error in errors] == ["$", "$.id", "$.items[0].code", "$.items[1]"]
    assert "'extra' was unexpected" in errors[0].reason
    assert "'code' is a required property" in errors[3].reason
    assert str(errors[1]) == "$.id: 'x' is not of type 'integer'"


def test_check_raises_with_every_error(store):
    with pytest.raises(SchemaValidationError) as error:
        store.check("sub/order.schema.json", {"items": [{}]})
    assert error.value.name == "sub/order.schema.json"
    assert error.value.errors == [
        FieldError("$", "'id' is a required property"),
        FieldError("$.items[0]", "'code' is a required property"),
    ]
    assert "$.items[0]: 'code' is a required property" in str(error.value)


def test_validate_against_a_definition(store):
    assert store.validate("base.schema.json", {"code": "OK"}, definition="item") == []
    assert [error.path for error in store.validate("base.schema.json", {"code": 1}, definition="item")] == ["$.code"]


def test_unknown_schema_or_definition(store):
    with pytest.raises(SchemaNotFound):
        store.validate("missing.schema.json", {})
    with pytest.raises(SchemaNotFound):
        store.validate("base.schema.json", {}, definition="missing")


def test_schema_returns_a_copy(store):
    copy = store.schema("base.schema.json")
    copy["$defs"].clear()
    assert "code" in store.schema("base.schema.json")["$defs"]


def test_rejects_mismatched_id(tmp_path):
    (tmp_path / "a.schema.json").write_text(json.dumps({"$schema": DIALECT, "$id": BASE_URI + "b.schema.json"}))
    with pytest.raises(SchemaDefinitionError, match=r"\$id"):
        SchemaStore(tmp_path)


def test_rejects_other_dialect(tmp_path):
    body = {"$schema": "http://json-schema.org/draft-07/schema#", "$id": BASE_URI + "a.schema.json"}
    (tmp_path / "a.schema.json").write_text(json.dumps(body))
    with pytest.raises(SchemaDefinitionError, match="2020-12"):
        SchemaStore(tmp_path)


def test_rejects_schema_violating_metaschema(tmp_path):
    write(tmp_path, "a.schema.json", {"type": "no-such-type"})
    with pytest.raises(SchemaDefinitionError, match="元 schema"):
        SchemaStore(tmp_path)


def test_rejects_invalid_json_and_missing_directory(tmp_path):
    with pytest.raises(SchemaDefinitionError, match="不存在"):
        SchemaStore(tmp_path / "missing")
    (tmp_path / "a.schema.json").write_text("{")
    with pytest.raises(SchemaDefinitionError, match="JSON"):
        SchemaStore(tmp_path)


def test_unresolvable_reference_is_a_definition_error(tmp_path):
    write(tmp_path, "a.schema.json", {"$ref": "missing.schema.json"})
    with pytest.raises(SchemaDefinitionError, match="引用"):
        SchemaStore(tmp_path).validate("a.schema.json", {})


def test_inline_removes_every_reference_and_keeps_verdicts(store):
    inlined = store.inline("sub/order.schema.json")
    assert "$ref" not in json.dumps(inlined)
    assert "$id" not in inlined
    validator = Draft202012Validator(inlined)
    assert validator.is_valid({"id": 1, "items": [{"code": "AB"}], "note": "ABC"})
    assert not validator.is_valid({"id": 1, "items": [{"code": "ab"}]})
    assert not validator.is_valid({"id": 1, "items": [], "note": "ABCD"})


def test_inline_rejects_recursive_schema(tmp_path):
    write(tmp_path, "tree.schema.json", {"type": "object", "properties": {"child": {"$ref": "#"}}})
    with pytest.raises(SchemaDefinitionError, match="循环"):
        SchemaStore(tmp_path).inline("tree.schema.json")
