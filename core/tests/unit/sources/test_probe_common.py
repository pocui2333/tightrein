import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from probe_world import NOW, RELEASE, CountingRandom, make_redactor, make_target

from tightrein.domain.enums import Probe, ProbeLevel, RunStatus, Source
from tightrein.sources.base import ProbeOutcome, failed, resolve_level, save_state, skipped
from tightrein.sources.common.raw import RawDir
from tightrein.sources.common.signals import SignalFactory, serialized_size

CONTEXT_LIMIT_BYTES = 16 * 1024
from tightrein.store.repos import incidental_sources, source_cursors
from tightrein.store.repos.incidental_sources import IncidentalSource
from tightrein.store.repos.source_cursors import SourceCursor


def test_target_rejects_unknown_environment(tmp_path):
    with pytest.raises(ValueError, match="environment"):
        make_target(tmp_path, environment="qa")


def test_outcome_status_and_reasons():
    with pytest.raises(ValueError, match="状态"):
        ProbeOutcome(RunStatus.RUNNING)
    with pytest.raises(ValueError, match="原因"):
        ProbeOutcome(RunStatus.SKIPPED)
    with pytest.raises(ValueError, match="notes"):
        ProbeOutcome(RunStatus.FAILED)
    assert skipped("没有新提交").skipped_reason == "没有新提交"
    assert failed("报告不完整").notes == ("报告不完整",)


@pytest.mark.parametrize(("probe", "level", "expected"), [
    (Probe.API_FUZZ, None, ProbeLevel.SHALLOW),
    (Probe.API_FUZZ, ProbeLevel.DEEP, ProbeLevel.DEEP),
    (Probe.STATIC, None, ProbeLevel.INCREMENTAL),
    (Probe.PLATFORM_ERRORS, ProbeLevel.DEEP, None),
    (Probe.INCIDENTAL, None, None),
])
def test_levels(probe, level, expected):
    assert resolve_level(probe, level) is expected


def test_level_not_available():
    with pytest.raises(ValueError, match="incremental、full"):
        resolve_level(Probe.STATIC, ProbeLevel.SHALLOW)


def test_save_state_writes_cursor_and_sources(conn):
    cursor = SourceCursor("abc", {"until": "2026-10-05T11:00:00Z"}, NOW, {"a.log": "2026-10-05T11:00:00"})
    source = IncidentalSource("data/runs/R-1/handoff/triage-P-0001.json", "f" * 64, NOW, 2)
    save_state(conn, ProbeOutcome(RunStatus.OK, cursors=(cursor,), sources=(source,)))
    assert source_cursors.get(conn, "abc") == cursor
    assert incidental_sources.TABLE.find(conn) == [source]


def test_redactor_headers_query_and_url():
    redactor = make_redactor()
    assert redactor.headers({"Authorization": "Bearer abc", "Cookie": "a=b", "Accept": "json"}) == {
        "Authorization": "[已脱敏]", "Cookie": "[已脱敏]", "Accept": "json"}
    assert redactor.query({"token": "abc", "page": "2"}) == {"token": "[已脱敏]", "page": "2"}
    assert redactor.url("https://h/api/x?token=abc&page=2") == "https://h/api/x?token=[已脱敏]&page=2"
    assert redactor.url("https://h/api/x") == "https://h/api/x"


def test_redactor_reproduce_keeps_the_placeholder():
    redactor = make_redactor()
    command = ("curl -X GET 'https://h/api/x' -H 'Authorization: Bearer eyJhbGciOi.eyJzdWIiOi.c2ln' "
               "-d '{\"password\": \"p\"}'")
    result = redactor.reproduce(command, ["eyJhbGciOi.eyJzdWIiOi.c2ln"])
    assert "-H 'Authorization: Bearer <TOKEN>'" in result
    assert "eyJ" not in result and '"password": "[已脱敏]"' in result


def test_redactor_excerpt_is_redacted_then_truncated():
    redactor = make_redactor("s3cret-value")
    text = "password=s3cret-value " + "x" * 600
    excerpt = redactor.excerpt(text)
    assert len(excerpt) == 500 and "s3cret" not in excerpt


def test_raw_dir_paths(tmp_path):
    raw = RawDir(tmp_path / "raw")
    raw.write_json("Company/summary.json", {"a": 1})
    raw.write_text("notes.txt", "x")
    assert raw.files() == ("Company/summary.json", "notes.txt")
    for bad in ("", "/etc/passwd", "../x", "a/../../x", "a\\b"):
        with pytest.raises(ValueError):
            raw.path(bad)


def factory(tmp_path, *secrets):
    return SignalFactory(make_target(tmp_path), Probe.API_FUZZ, make_redactor(*secrets), randomness=CountingRandom())


def test_signal_fields(tmp_path):
    occurred = datetime(2026, 10, 5, 11, 15, 3, 999000, tzinfo=timezone(timedelta(hours=8)))
    signal = factory(tmp_path).create(
        source=Source.SYNTHETIC, check="not_a_server_error", location="POST /api/Order/Query",
        message="服务端返回 500", occurred_at=occurred, release=RELEASE, context={"role": "Company"},
        actor={"id": "Company", "role": "Company"})
    assert re.fullmatch(r"S-[0-9A-HJKMNP-TV-Z]{26}", signal.id)
    assert signal.run_id == "R-20261005-030000-collect-api-fuzz"
    assert (signal.probe, signal.environment, signal.suppressed) == (Probe.API_FUZZ, "staging", False)
    assert signal.occurred_at == datetime(2026, 10, 5, 3, 15, 3, tzinfo=timezone.utc)
    assert signal.fingerprint is None and signal.normalized_message is None


def test_signal_ids_differ_and_message_is_redacted_and_truncated(tmp_path):
    make = factory(tmp_path, "hunter2")
    first = make.create(source=Source.ERROR, check="error", location="A.B", message="password hunter2 " + "y" * 2000,
                        occurred_at=NOW, release=None, context={})
    second = make.create(source=Source.ERROR, check="error", location="A.B", message="m", occurred_at=NOW,
                         release=None, context={})
    assert first.id != second.id
    assert len(first.message) == 1000 and "hunter2" not in first.message


def test_context_is_redacted_but_reproduce_is_kept(tmp_path):
    signal = factory(tmp_path).create(
        source=Source.SYNTHETIC, check="c", location="GET /a", message="m", occurred_at=NOW, release=None,
        context={"reproduce": "curl -H 'Authorization: Bearer <TOKEN>'", "pageUrl": "/a?token=abc",
                 "request": {"cookie": "x"}})
    assert signal.context["reproduce"] == "curl -H 'Authorization: Bearer <TOKEN>'"
    assert signal.context["pageUrl"] == "/a?token=[已脱敏]"
    assert signal.context["request"] == {"cookie": "[已脱敏]"}


def test_large_context_items_move_to_raw(tmp_path):
    make = factory(tmp_path)
    big = {"rows": ["x" * 100] * 300}
    signal = make.create(source=Source.SYNTHETIC, check="c", location="GET /a", message="m", occurred_at=NOW,
                         release=None, context={"role": "Company", "body": big, "small": 1})
    assert serialized_size(signal.context) <= CONTEXT_LIMIT_BYTES
    reference = signal.context["bodyRef"]
    assert reference == f"refs/{signal.id}-body.json"
    assert json.loads((tmp_path / "raw" / "api-fuzz" / reference).read_text(encoding="utf-8")) == big
    assert signal.context["role"] == "Company" and "body" not in signal.context


def test_empty_location_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="location"):
        factory(tmp_path).create(source=Source.SYNTHETIC, check="c", location="", message="m", occurred_at=NOW,
                                 release=None, context={})
