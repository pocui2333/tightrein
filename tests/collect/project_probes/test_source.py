import json
from datetime import timedelta

import pytest

from tightrein.collect.common.source import SourceMisconfigured, SourceStatus
from tightrein.collect.project_probes import source
from tightrein.protocol.process import SubprocessRunner
from tightrein.store.tables import state

GOOD = """
import json, os, sys
found = json.load(sys.stdin)
signals = [{"location": "job:import", "symptom": "导入没有按时完成", "evidence": [f"窗口 {found['window']['since']}"],
            "severityHint": "P1", "fingerprint": "missed:import",
            "context": {"last": found["state"], "secret": os.environ.get("TIGHTREIN_SECRET_DEMO_READONLY")}}]
print(json.dumps({"signals": signals, "state": {"seen": (found["state"] or {}).get("seen", 0) + 1},
                  "notes": ["检查完成"]}))
"""
BAD = "import json, sys; sys.stdin.read(); print(json.dumps({'signals': [{'location': 'x'}]}))"
CRASH = "import sys; sys.stdin.read(); sys.stderr.write('token=secret-value'); sys.exit(3)"


def make(source_runtime, scripts, **registration):
    current = source_runtime(modules={source.SOURCE: {"status": "enabled"}}, runner=SubprocessRunner(),
                             controls={source.SOURCE: {"probes": [
                                 {"name": name, "command": ["{python}", f"scripts/{name}.py"], "every": "1h",
                                  **registration} for name in scripts]}},
                             secrets={"demo.readonly": "secret-value"},
                             sites={"target": {"baseUrl": "https://demo.example.test", "environment": "staging"}})
    folder = current.workspace.root / "scripts"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in scripts.items():
        (folder / f"{name}.py").write_text(text, encoding="utf-8")
    return current


def save(current, result):
    for key, value in result.state.items():
        state.put(current.conn, key, value, current.clock)


def test_registrations_and_due():
    found = source.registered({"probeTimeout": "5m", "probes": [
        {"name": "a", "command": ["x"], "every": "15m"}, {"name": "b", "command": ["y"], "every": "2d", "timeout": "1m",
                                                         "secrets": ["s"]}]})
    assert [(item.every, item.timeout_s, item.secrets) for item in found] == [
        (timedelta(minutes=15), 300.0, ()), (timedelta(days=2), 60.0, ("s",))]
    with pytest.raises(SourceMisconfigured, match="probes\\[0\\]"):
        source.registered({"probeTimeout": "5m", "probes": [{"name": "a", "every": "1w"}]})
    with pytest.raises(SourceMisconfigured, match="没有登记探针 c"):
        source.find(found, "c")


def test_a_probe_runs_its_output_becomes_signals_and_its_state_is_returned(source_runtime):
    current = make(source_runtime, {"daily-import": GOOD}, secrets=["demo.readonly"])
    result = source.collect(current)
    assert result.status is SourceStatus.DONE and result.coverage == ["daily-import"]
    [signal] = result.signals
    assert (signal.check_type, signal.location, signal.message, signal.severity_hint) == (
        "probe", "job:import", "导入没有按时完成", "P1")
    assert signal.group_key == "daily-import:missed:import" and signal.environment == "staging"
    assert signal.evidence["facts"] == ["窗口 2026-10-04T03:00:00Z"] and signal.evidence["sourceName"] == "daily-import"
    assert "secret-value" not in json.dumps(signal.evidence)
    assert result.state == {"collect.project_probes:daily-import": {"lastRunAt": "2026-10-05T03:00:00Z",
                                                                    "state": {"seen": 1}}}
    assert "daily-import：检查完成" in result.notes
    save(current, result)
    assert source.collect(current).reason == source.NONE_DUE
    again = source.collect(current, names=["daily-import"])
    assert again.signals[0].evidence["details"]["last"] == {"seen": 1}
    current.clock.advance(timedelta(hours=1))
    later = source.collect(current)
    assert later.signals[0].evidence["facts"] == ["窗口 2026-10-05T03:00:00Z"]


def test_invalid_or_crashing_probes_are_voided_and_keep_no_state(source_runtime):
    current = make(source_runtime, {"good": GOOD, "bad": BAD, "crash": CRASH})
    result = source.collect(current)
    assert result.status is SourceStatus.PARTIAL and list(result.state) == ["collect.project_probes:good"]
    assert any(note.startswith("项目探针 bad 本次作废：invalid：输出不符合探针契约") for note in result.notes)
    assert any(note.startswith("项目探针 crash 本次作废：unavailable：crash 退出码 3") for note in result.notes)
    stderr = (current.workspace.run_dir(current.run) / "11-collect.project_probes-raw" / "crash.stderr.log")
    assert "secret-value" not in stderr.read_text(encoding="utf-8")
    only_bad = make(source_runtime, {"bad": BAD})
    failed = source.collect(only_bad)
    assert failed.status is SourceStatus.FAILED and failed.state == {}


def test_disabled_unregistered_and_trial(source_runtime):
    assert source.collect(source_runtime()).status is SourceStatus.SKIPPED
    empty = source_runtime(modules={source.SOURCE: {"status": "enabled"}})
    assert source.collect(empty).reason == source.DISABLED
    current = make(source_runtime, {"daily-import": GOOD, "bad": BAD})
    trial = source.trial(current, "daily-import")
    assert trial.error is None and len(trial.signals) == 1
    assert state.get(current.conn, "collect.project_probes:daily-import") is None
    assert source.trial(current, "bad").error.startswith("invalid：")
