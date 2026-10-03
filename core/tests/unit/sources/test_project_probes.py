import io
import json
import sys
from datetime import timedelta

import pytest
from probe_world import NOW, RELEASE, CountingRandom, make_redactor, make_target, open_db

from tightrein.domain.enums import RunStatus, Source
from tightrein.sources.base import ProbeOptions
from tightrein.sources.project_probes import helpers, registry
from tightrein.sources.project_probes.source import DISABLED, NONE_DUE, ProjectProbeDependencies, ProjectProbeSource
from tightrein.store.repos import probe_states
from tightrein.store.repos.probe_states import ProbeState

GOOD = """
import json, sys
found = json.load(sys.stdin)
signals = [{"location": "job:import", "symptom": "导入没有按时完成", "evidence": [f"窗口 {found['window']['since']}"],
            "severityHint": "P1", "fingerprint": "missed:import", "context": {"last": found["state"]}}]
print(json.dumps({"signals": signals, "state": {"seen": (found["state"] or {}).get("seen", 0) + 1},
                  "notes": ["检查完成"]}))
"""
BAD = "import json, sys; sys.stdin.read(); print(json.dumps({'signals': [{'location': 'x'}]}))"
CRASH = "import sys; sys.stdin.read(); sys.stderr.write('token=secret-value'); sys.exit(3)"


def registration(name, script, every="1h", **extra):
    return {"name": name, "command": ["{python}", f"probes/{name}.py"], "every": every, **extra}


def make_source(tmp_path, make_config, scripts):
    workspace = tmp_path / "workspace"
    (workspace / "probes").mkdir(parents=True)
    for name, text in scripts.items():
        (workspace / "probes" / f"{name}.py").write_text(text, encoding="utf-8")
    config = make_config(sources={"project-probes": [registration(name, text) for name, text in scripts.items()]})
    conn = open_db(tmp_path)
    return ProjectProbeSource(ProjectProbeDependencies(config, conn, workspace, make_redactor("secret-value"),
                                                       lambda at: RELEASE, environ={"PATH": "/bin"},
                                                       randomness=CountingRandom())), conn


def run(tmp_path, found, **options):
    return found.run(make_target(tmp_path, "project-probe"), None, ProbeOptions(**options))


def test_interval_and_due():
    assert registry.interval("15m") == timedelta(minutes=15) and registry.interval("2d") == timedelta(days=2)
    with pytest.raises(ValueError):
        registry.interval("1w")


def test_a_probe_runs_its_output_becomes_signals_and_its_state_is_returned(tmp_path, make_config):
    found, conn = make_source(tmp_path, make_config, {"daily-import": GOOD})
    outcome = run(tmp_path, found)
    assert outcome.status is RunStatus.OK and outcome.coverage.sources == ("daily-import",)
    [signal] = outcome.signals
    assert (signal.source, signal.check, signal.location, signal.message) == (
        Source.BEHAVIOR, "daily-import", "job:import", "导入没有按时完成")
    assert signal.context["probeFingerprint"] == "missed:import" and signal.context["severityHint"] == "P1"
    assert signal.context["evidence"] == ["窗口 2026-10-04T03:00:00Z"] and signal.release == RELEASE
    assert outcome.probe_states == (ProbeState("daily-import", NOW, {"seen": 1}),)
    assert "daily-import：检查完成" in outcome.notes
    probe_states.save(conn, outcome.probe_states[0])
    assert run(tmp_path, found).skipped_reason == NONE_DUE
    again = run(tmp_path, found, names=("daily-import",))
    assert again.signals[0].context["details"] == {"last": {"seen": 1}}


def test_invalid_or_crashing_probes_are_voided_and_keep_no_state(tmp_path, make_config):
    found, _ = make_source(tmp_path, make_config, {"good": GOOD, "bad": BAD, "crash": CRASH})
    outcome = run(tmp_path, found)
    assert outcome.status is RunStatus.PARTIAL and [state.name for state in outcome.probe_states] == ["good"]
    assert any(note.startswith("项目探针 bad 本次作废：输出不符合探针契约") for note in outcome.notes)
    assert any(note.startswith("项目探针 crash 本次作废：退出码 3") for note in outcome.notes)
    stderr = (tmp_path / "raw" / "project-probe" / "crash.stderr.log").read_text(encoding="utf-8")
    assert "secret-value" not in stderr
    only_bad, _ = make_source(tmp_path / "b", make_config, {"bad": BAD})
    assert run(tmp_path / "b", only_bad).status is RunStatus.FAILED


def test_disabled_unknown_and_trial(tmp_path, make_config):
    empty = ProjectProbeSource(ProjectProbeDependencies(make_config(), open_db(tmp_path), tmp_path, make_redactor(),
                                                        lambda at: None))
    assert run(tmp_path, empty).skipped_reason == DISABLED
    found, conn = make_source(tmp_path / "w", make_config, {"daily-import": GOOD})
    assert run(tmp_path, found, names=("nope",)).status is RunStatus.FAILED
    trial = found.trial("daily-import", make_target(tmp_path, "project-probe"))
    assert trial.run.ok and len(trial.signals) == 1 and probe_states.get(conn, "daily-import") is None


def test_helpers_build_check_and_emit_the_output(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"name": "x"})))
    assert helpers.read_input() == {"name": "x"}
    stream = io.StringIO()
    item = helpers.signal("job:a", "没跑", ["最近一次 01:00"], "missed:a", severity_hint="P2")
    helpers.emit([item], state={"k": 1}, notes=["n"], stream=stream)
    assert json.loads(stream.getvalue()) == {"signals": [item], "state": {"k": 1}, "notes": ["n"]}
    with pytest.raises(ValueError, match="契约"):
        helpers.emit([{"location": "x"}], stream=io.StringIO())
    monkeypatch.setenv("TIGHTREIN_PROBE_KEYCHAIN", "allowed")
    with pytest.raises(PermissionError, match="没有在"):
        helpers.secret("other")
