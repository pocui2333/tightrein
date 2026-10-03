import json
import zipfile
from datetime import datetime, timezone

from page_world import ENCODED_TOKEN, TOKEN, results, trace_bytes, write_results
from probe_world import make_redactor

from tightrein.pipeline.checks.pages import artifacts, failures, result_parser


def parsed(tmp_path, **options):
    return result_parser.parse(write_results(tmp_path / "raw" / "pages", **options))


def test_parse_recorded_results(tmp_path):
    report = parsed(tmp_path)
    assert report.complete and report.unknown == 0
    assert [(case.project, case.outcome) for case in report.cases] == [
        ("setup-Company", "expected"), ("setup-Admin", "unexpected"), ("e2e-Company", "expected"),
        ("e2e-Company", "flaky"), ("e2e-Company", "unexpected")]
    failure = report.cases[-1]
    assert (failure.title, failure.file, failure.role, failure.tags) == (
        "首页有统计卡片", "Company/broken.spec.ts", "Company", ("@browse",))
    assert failure.failed_step == "查看统计卡片" and failure.retries == 3
    assert failure.error.startswith("Error: expect(locator).toBeVisible() failed") and "\x1b" not in failure.error
    assert len(failure.observations) == 3 and len(failure.screenshots) == 3 and len(failure.traces) == 1
    assert failure.started_at == datetime(2026, 9, 29, 22, 55, 55, 420000, tzinfo=timezone.utc)
    assert report.cases[1].setup and not failure.setup


def test_incomplete_results(tmp_path):
    assert not parsed(tmp_path, trailing='{"title": "半行').complete
    assert not result_parser.parse(tmp_path / "none.ndjson").complete
    assert not parsed(tmp_path, items=[]).complete
    broken = [{"title": "x"}, *results(tmp_path / "raw" / "pages")]
    report = parsed(tmp_path, items=broken)
    assert report.complete and report.unknown == 1


def test_failures_per_case_and_page(tmp_path):
    found = failures.collect(parsed(tmp_path))
    counted = {}
    for item in found.failures:
        counted[(item.kind, item.case)] = counted.get((item.kind, item.case), 0) + 1
    assert counted == {("case-failure", "首页有统计卡片"): 1,
                       ("console-error", "首页标题"): 2, ("failed-request", "首页标题"): 1,
                       ("console-error", "首页有统计卡片"): 2, ("failed-request", "首页有统计卡片"): 1}
    failure = next(item for item in found.failures if item.kind == "case-failure")
    assert failure.page == "/home" and failure.message.startswith("Error: expect(locator).toBeVisible() failed")
    request = next(item for item in found.failures if item.kind == "failed-request")
    assert request.message == "500 GET /api/Stats"
    assert found.failed_setups["Admin"].startswith("TimeoutError: page.waitForURL")
    assert found.executed_roles == frozenset({"Company"})


def test_trace_cleaning_keeps_json_valid(tmp_path):
    path = tmp_path / "test-results" / "case-retry1" / "trace.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(trace_bytes())
    (tmp_path / "test-results" / "bad").mkdir()
    (tmp_path / "test-results" / "bad" / "trace.zip").write_bytes(b"not a zip")
    notes = artifacts.clean_traces(tmp_path, make_redactor())
    assert notes == ["trace test-results/bad/trace.zip 清除凭证失败(BadZipFile)，已删除"]
    assert not (tmp_path / "test-results" / "bad" / "trace.zip").exists()
    with zipfile.ZipFile(path) as archive:
        texts = {name: archive.read(name) for name in archive.namelist()}
    for name, data in texts.items():
        assert TOKEN.encode() not in data and ENCODED_TOKEN.encode() not in data and b"cookie-secret" not in data
    context = json.loads(texts["1-trace.trace"])
    state = context["options"]["storageState"]
    assert state["origins"][0]["localStorage"] == [{"name": "auth", "value": "[已脱敏]"}]
    assert state["cookies"][0] == {"name": "sid", "value": "[已脱敏]", "domain": "127.0.0.1"}
    headers = json.loads(texts["1-trace.network"])["snapshot"]["request"]["headers"]
    assert headers == [{"name": "Authorization", "value": "[已脱敏]"}, {"name": "Accept", "value": "*/*"}]
    assert json.loads(texts["resources/login.json"]) == {"data": {"token": "[已脱敏]"}}
    assert texts["screencast/1.jpeg"] == b"\xff\xd8\xff\xe0binary"


def test_trace_cleaning_removes_a_registered_static_header_credential():
    code = "code-7f3a9c41"
    line = json.dumps({"snapshot": {"request": {"url": f"https://h/?code={code}", "headers": [
        {"name": "X-Access-Code", "value": code}]}}})
    cleaned = artifacts.clean_text(line, make_redactor(code))
    assert code not in cleaned
    assert json.loads(cleaned)["snapshot"]["request"]["headers"] == [{"name": "X-Access-Code", "value": "[已脱敏]"}]
