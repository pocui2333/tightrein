import re

import pytest

from tightrein.collect.dedup.normalize import MAX_LENGTH, Rule, RuleInvalid, line_of, location, message, rules_from


def test_guid():
    assert message("user 3f2a8b1c-1d2e-4f50-9a6b-7c8d9e0f1a2b missing") == "user <guid> missing"


def test_iso_time_and_clock_time():
    assert message("at 2026-09-29T02:15:03Z and 02:15:03") == "at <time> and <time>"


def test_hex_before_number():
    assert message("commit d6f37025 hash a1234567ff") == "commit <hex> hash <hex>"


def test_long_number_but_not_short():
    assert message("order 123456 page 12 status 404") == "order <num> page 12 status 404"


def test_windows_and_posix_paths():
    text = r"cannot open C:\APP\online_services\upload\a.dat or /tmp/x/y.log"
    assert message(text) == "cannot open <path> or <path>"


def test_quoted_values():
    assert message("field 'abc' invalid, got \"xyz\"") == "field <value> invalid, got <value>"


def test_project_rules_run_after_defaults():
    rules = (Rule(re.compile(r"tenant-[a-z]+"), "<tenant>"),)
    assert message("tenant-alpha failed", rules) == "<tenant> failed"
    # 内置规则先把 4 位以上数字换掉，项目规则看到的是替换后的文字
    assert message("tenant-12345", (Rule(re.compile(r"tenant-<num>"), "<tenant>"),)) == "<tenant>"


def test_truncates_to_max_length_and_collapses_whitespace():
    assert len(message("x" * 500)) == MAX_LENGTH == 200
    assert message("  a \n\t b  ") == "a b"


def test_project_rules_are_checked_once():
    assert rules_from([{"pattern": "a+", "replacement": "<a>"}])[0].replacement == "<a>"
    with pytest.raises(RuleInvalid):
        rules_from([{"pattern": "(", "replacement": "x"}])
    with pytest.raises(RuleInvalid):
        rules_from([{"pattern": "x"}])


def test_api_location_keeps_template_and_replaces_ids():
    assert location("post /api/Material/123?x=1") == "POST /api/Material/{id}"
    assert location("GET /api/Order/{id}") == "GET /api/Order/{id}"
    assert location("DELETE /api/User/3f2a8b1c-1d2e-4f50-9a6b-7c8d9e0f1a2b") == "DELETE /api/User/{id}"


def test_page_location_replaces_ids():
    assert location("/newhome/CompanyDetail/42?tab=a") == "/newhome/CompanyDetail/:id"
    assert location("/newhome/calculate") == "/newhome/calculate"


def test_code_location_drops_line_and_column():
    assert location("Services/MaterialService.cs:118") == "Services/MaterialService.cs"
    assert location("Services/MaterialService.cs:118:7") == "Services/MaterialService.cs"
    assert location("Services/A.cs:A.Query:118") == "Services/A.cs:A.Query"
    assert location(None) is None


def test_line_of_code_locations_only():
    assert line_of("app.py:42") == 42
    assert line_of("app.py:42:7") == 42
    assert line_of("GET /api/x/12") is None
    assert line_of("/page/3") is None
    assert line_of("app.py") is None
