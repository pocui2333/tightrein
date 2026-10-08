import io
import json
import sys

import pytest

from tightrein.collect.project_probes import helpers


def test_helpers_build_check_and_emit_the_output(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"name": "x"})))
    assert helpers.read_input() == {"name": "x"}
    stream = io.StringIO()
    item = helpers.signal("job:a", "没跑", ["最近一次 01:00"], "missed:a", severity_hint="P2",
                          occurred_at="2026-10-05T01:00:00Z", context={"k": 1})
    helpers.emit([item], state={"k": 1}, notes=["n"], stream=stream)
    assert json.loads(stream.getvalue()) == {"signals": [item], "state": {"k": 1}, "notes": ["n"]}
    with pytest.raises(ValueError, match="契约"):
        helpers.emit([{"location": "x"}], stream=io.StringIO())


def test_only_registered_secrets_are_readable_and_get_redacted(monkeypatch):
    with pytest.raises(PermissionError, match="没有在"):
        helpers.secret("other")
    monkeypatch.setenv("TIGHTREIN_SECRET_DEMO_READONLY", "pw-1234567")
    assert helpers.secret("demo.readonly") == "pw-1234567"
    assert "pw-1234567" not in helpers.redact("连接用 pw-1234567 失败")
    assert "pw-1234567" not in helpers.signal("a", "用 pw-1234567", ["pw-1234567"], "f")["evidence"][0]
