from __future__ import annotations

import json
import zipfile
from pathlib import Path

from tightrein.implement.check.runtime import traces
from tightrein.protocol.security import Redactor


def _redactor() -> Redactor:
    redactor = Redactor()
    redactor.register("static-header-token-123", "credential")
    return redactor


def test_trace_cleaning_keeps_json_valid() -> None:
    record = {"type": "resource-snapshot", "snapshot": {"request": {"headers": [
        {"name": "Authorization", "value": "Bearer abc.def.ghi"}, {"name": "Accept", "value": "text/html"}]},
        "response": {"headers": [{"name": "set-cookie", "value": "sid=xyz"}]}},
        "storageState": {"cookies": [{"name": "sid", "value": "xyz"}],
                         "origins": [{"origin": "http://x", "localStorage": [{"name": "token", "value": "t0k"}]}]}}
    cleaned = traces.clean_text(json.dumps(record) + "\n" + json.dumps({"note": "plain"}), _redactor())
    lines = [json.loads(line) for line in cleaned.splitlines()]
    headers = lines[0]["snapshot"]["request"]["headers"]
    assert headers[0]["value"] == traces.MASK and headers[1]["value"] == "text/html"
    assert lines[0]["snapshot"]["response"]["headers"][0]["value"] == traces.MASK
    assert lines[0]["storageState"]["cookies"][0]["value"] == traces.MASK
    assert lines[0]["storageState"]["origins"][0]["localStorage"][0]["value"] == traces.MASK
    assert lines[1] == {"note": "plain"}


def test_trace_cleaning_removes_a_registered_static_header_credential(tmp_path: Path) -> None:
    trace = tmp_path / "test-results" / "case" / traces.TRACE_NAME
    trace.parent.mkdir(parents=True)
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.network", json.dumps({"headers": [{"name": "X-Api-Key",
                                                                      "value": "static-header-token-123"}]}))
        archive.writestr("resources/page.html", "<p>static-header-token-123</p>")
        archive.writestr("resources/shot.png", b"\x89PNG\x00\x01")
    broken = tmp_path / "other" / traces.TRACE_NAME
    broken.parent.mkdir()
    broken.write_bytes(b"not a zip")
    notes = traces.clean_all(tmp_path, _redactor())
    with zipfile.ZipFile(trace) as archive:
        joined = archive.read("0-trace.network").decode() + archive.read("resources/page.html").decode()
        assert "static-header-token-123" not in joined
        assert archive.read("resources/shot.png") == b"\x89PNG\x00\x01"
    # 改写不了的直接删除
    assert not broken.exists() and len(notes) == 1 and "已删除" in notes[0]
