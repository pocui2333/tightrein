import pytest

from tightrein.contracts.validate import FieldError
from tightrein.runner.output import NO_JSON, check_output, extract_json, retry_note

SCHEMA = "runner/roles/claim-verifier.schema.json"
FENCE = "`" * 3


@pytest.mark.parametrize("text, expected", [
    ('{"verdict": "confirmed"}', {"verdict": "confirmed"}),
    ('  [1, 2]  ', [1, 2]),
    (f'结论如下：\n{FENCE}json\n{{"a": 1}}\n{FENCE}\n再给一个：\n{FENCE}json\n{{"a": 2}}\n{FENCE}\n', {"a": 2}),
    ('我认为 {"verdict": "refuted", "note": "括号 } 在字符串里"} 就是结论', {"verdict": "refuted", "note": "括号 } 在字符串里"}),
    ('先 {"a": 1} 后 {"b": {"c": 2}} 完', {"b": {"c": 2}}),
    (f'{FENCE}json\n{{坏的}}\n{FENCE}\n但是 {{"ok": true}}', {"ok": True}),
    ('转义 {"q": "a \\" b"}', {"q": 'a " b'}),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


@pytest.mark.parametrize("text", [None, "", "没有 JSON", "{没闭合", f"{FENCE}json\n{{坏}}\n{FENCE}"])
def test_no_json(text):
    assert extract_json(text) is None


def test_structured_output_is_preferred():
    checked = check_output("runner/tasks/triage-dedup.schema.json", {"x": 1}, '{"y": 2}')
    assert checked.value == {"x": 1}
    assert not checked.ok


def test_missing_and_non_object_outputs():
    assert check_output(SCHEMA, None, "没有结果").errors == (NO_JSON,)
    assert check_output(SCHEMA, None, "[1, 2]").errors == (FieldError("$", "结果须为 JSON 对象"),)


def test_errors_carry_json_paths():
    checked = check_output(SCHEMA, None, '{"verdict": "maybe"}')
    assert not checked.ok
    assert all(error.path.startswith("$") for error in checked.errors)


def test_retry_note_lists_errors_and_the_raw_output():
    note = retry_note(SCHEMA, '{"verdict": "maybe"}', [FieldError("$.verdict", "'maybe' is not one of [...]")],
                      8000)
    assert note.splitlines()[0].startswith(f"上一次的输出不符合 {SCHEMA}")
    assert "- $.verdict: 'maybe' is not one of [...]" in note
    assert note.endswith('{"verdict": "maybe"}')
    assert retry_note(SCHEMA, None, [NO_JSON], 8000).endswith("(空)")
