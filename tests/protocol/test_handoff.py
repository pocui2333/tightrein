import json
from pathlib import Path

import pytest
from jsonschema.exceptions import SchemaError

from tightrein.protocol import handoff
from tightrein.protocol.handoff import FactsInvalid, Handoff, Metrics, Status, Tokens, Versions

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["files", "tests", "deviation"],
    "properties": {
        "files": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "tests": {"type": "array", "items": {"type": "string"}},
        "deviation": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}


def sample(**changes: object) -> Handoff:
    values: dict = {
        "point": "implement.code",
        "subject": "0018",
        "run": "R-20261007T093000Z-implement",
        "status": Status.PASSED,
        "summary": "改了订单查询的归属过滤",
        "facts": {"files": ["app/orders.py"], "tests": ["tests/test_orders.py"], "deviation": None, "camelCase": 1},
        "metrics": Metrics(duration_ms=1200, calls=1, tokens=Tokens(input=1000, output=200, cache_read=800)),
        "notes": "先看了调用方",
        "round": 2,
        "created_at": "2026-10-07T09:30:00Z",
        "versions": Versions(tightrein="abc1234", prompt="p1", settings="s1", tool="claude", model="opus"),
    }
    values.update(changes)
    return Handoff(**values)


def test_the_json_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "35-implement.code.r2-handoff.json"
    original = sample()
    handoff.write(path, original)
    assert handoff.read(path) == original


def test_fields_are_camel_case_and_facts_are_kept_as_written(tmp_path: Path) -> None:
    path = tmp_path / "handoff.json"
    handoff.write(path, sample())
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["metrics"]["durationMs"] == 1200 and data["metrics"]["tokens"]["cacheRead"] == 800
    assert data["versions"]["toolVersion"] is None and data["createdAt"] == "2026-10-07T09:30:00Z"
    assert data["facts"]["camelCase"] == 1 and data["status"] == "passed"


def test_fields_that_do_not_apply_are_written_as_null(tmp_path: Path) -> None:
    path = tmp_path / "handoff.json"
    handoff.write(path, sample(metrics=Metrics(), notes=None))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["notes"] is None
    assert set(data["metrics"]) >= {"durationMs", "filesChanged", "linesChanged", "produced", "passed", "failed"}
    assert all(value is None for value in data["metrics"].values())


def test_the_file_is_utf8_with_two_space_indent_and_a_final_newline(tmp_path: Path) -> None:
    path = tmp_path / "handoff.json"
    handoff.write(path, sample())
    text = path.read_text(encoding="utf-8")
    assert text.endswith("}\n") and '\n  "point": "implement.code"' in text and "改了订单查询" in text


def test_the_next_step_gets_only_the_first_three_parts() -> None:
    assert sample().for_next_step() == {
        "status": "passed",
        "summary": "改了订单查询的归属过滤",
        "facts": sample().facts,
    }


def test_long_notes_are_cut(tmp_path: Path) -> None:
    path = tmp_path / "handoff.json"
    handoff.write(path, sample(notes="字" * (handoff.NOTES_LIMIT + 10)))
    notes = handoff.read(path).notes or ""
    assert notes.startswith("字" * handoff.NOTES_LIMIT) and notes.endswith("(备注已截断)")


def test_required_facts_are_checked_and_every_error_is_listed_at_once() -> None:
    handoff.check_facts("implement.code", {"files": ["a.py"], "tests": [], "deviation": None}, SCHEMA)
    with pytest.raises(FactsInvalid) as raised:
        handoff.check_facts("implement.code", {"files": [], "tests": "none", "extra": 1}, SCHEMA)
    errors = raised.value.errors
    assert len(errors) == 4
    assert any("deviation" in item and item.startswith("/：") for item in errors)
    assert any("extra" in item for item in errors)
    assert any(item.startswith("/files：") for item in errors)
    assert any(item.startswith("/tests：") for item in errors)
    assert raised.value.point == "implement.code"


def test_weighted_tokens_count_cache_reads_at_a_fraction() -> None:
    tokens = Tokens(input=1000, output=200, cache_read=800, cache_write=100)
    # input 已含缓存读取与写入：只把其中的缓存读取换成按权重计
    assert tokens.weighted(0.1) == 1000 - 800 + 200 + 80
    tokens.add(Tokens(input=1, output=1, cache_read=1, cache_write=1))
    assert (tokens.input, tokens.output, tokens.cache_read, tokens.cache_write) == (1001, 201, 801, 101)


def test_schemas_must_be_valid_2020_12(tmp_path: Path) -> None:
    good = tmp_path / "good.schema.json"
    good.write_text(json.dumps(SCHEMA), encoding="utf-8")
    assert handoff.load_schema(good) == SCHEMA
    bad = tmp_path / "bad.schema.json"
    bad.write_text(json.dumps({"type": "nothing"}), encoding="utf-8")
    with pytest.raises(SchemaError):
        handoff.load_schema(bad)


def test_schema_ids_must_match_the_file_and_references_must_resolve(tmp_path: Path) -> None:
    named = tmp_path / "step.schema.json"
    named.write_text(json.dumps({**SCHEMA, "$id": "https://example.invalid/other.schema.json"}), encoding="utf-8")
    with pytest.raises(handoff.SchemaDefinitionError, match="与文件名不一致"):
        handoff.load_schema(named)
    named.write_text(json.dumps({**SCHEMA, "$id": "https://example.invalid/step.schema.json"}), encoding="utf-8")
    assert handoff.load_schema(named)["$id"].endswith("step.schema.json")
    refs = tmp_path / "refs.schema.json"
    defs = {"$defs": {"name": {"type": "string"}}}
    refs.write_text(json.dumps({**defs, "properties": {"a": {"$ref": "#/$defs/name"}}}), encoding="utf-8")
    assert handoff.load_schema(refs)["properties"]["a"] == {"$ref": "#/$defs/name"}
    for reference in ("#/$defs/missing", "other.schema.json#/x"):
        refs.write_text(json.dumps({**defs, "properties": {"a": {"$ref": reference}}}), encoding="utf-8")
        with pytest.raises(handoff.SchemaDefinitionError, match="引用解析不了"):
            handoff.load_schema(refs)
