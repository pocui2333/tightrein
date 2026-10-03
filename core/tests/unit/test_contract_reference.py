import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "dev" / "contract_reference.py"
SPEC = importlib.util.spec_from_file_location("contract_reference", SCRIPT)
contract_reference = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract_reference)


def test_the_generated_references_match_the_schemas():
    for target, (file_name, generate) in contract_reference.TARGETS.items():
        path = contract_reference.REFERENCE_DIR / file_name
        assert path.read_text(encoding="utf-8") == generate(), \
            f"{path} 已过期：在 core 目录执行 .venv/bin/python dev/contract_reference.py {target}"


def test_nested_fields_follow_references_and_array_items():
    schema = {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "array", "description": "列表", "items": {"type": "object", "required": ["b"], "properties": {
            "b": {"$ref": "../common.schema.json#/$defs/issueId", "description": "编号", "examples": ["0001"]}}}}}}
    assert contract_reference.fields(schema, "handoff/x.schema.json") == [
        ("`a`", "数组", "是", "列表", ""),
        ("`a[].b`", "字符串(`^\\d{4,}$`)", "是", "编号", '`"0001"`')]
