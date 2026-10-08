import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tightrein.agents.params import Access, CallParams, Limits, Model
from tightrein.agents.result import CallStatus
from tightrein.agents.tools import CallConfigError, Parsed
from tightrein.agents.tools.claude import ClaudeAdapter
from tightrein.agents.tools.replay import ReplayAdapter, call_sha256, index_entry
from tightrein.protocol.process import Command, Outcome

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
NOW = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)
OPUS = Model("opus", "claude", "opus")


def params(workdir: Path = Path("/ws/worktrees/readonly"), **changes) -> CallParams:
    base = CallParams(
        point="assess.triage", run="R-20261007T050000Z-assess", subject="P-0042", model=OPUS, fallback=None,
        prompt="判断以下主张是否成立", schema={"type": "object"}, workdir=workdir,
        limits=Limits(600, 40, 16_000, 50_000, 300),
    )
    return replace(base, **changes)


class FakeRunner:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.commands: list[Command] = []

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        return Outcome(self.exit_code, "", "" if self.exit_code == 0 else "patch does not apply", 1, None, None)


def record(root: Path, call_params: CallParams, call: int = 1, *, stdout: str | None = None,
           patch: str | None = None) -> None:
    name = f"{call_params.point}-{call_params.subject}.{call}"
    directory = root / name
    directory.mkdir(parents=True)
    text = stdout if stdout is not None else (FIXTURES / "stream-success.jsonl").read_text(encoding="utf-8")
    (directory / "stdout.jsonl").write_text(text, encoding="utf-8")
    if patch is not None:
        (directory / "changes.patch").write_text(patch, encoding="utf-8")
    index = root / "index.json"
    entries = json.loads(index.read_text(encoding="utf-8"))["recordings"] if index.exists() else []
    entries.append(index_entry(call_params, call, name, "claude"))
    index.write_text(json.dumps({"recordings": entries}), encoding="utf-8")


def replay(root: Path, runner: FakeRunner | None = None) -> ReplayAdapter:
    return ReplayAdapter(root, {"claude": ClaudeAdapter()}, runner or FakeRunner())


def play(adapter: ReplayAdapter, call_params: CallParams, on_line=None) -> tuple[Outcome, Parsed]:
    command = adapter.build(call_params, OPUS, executable="replay", env={}, schema=None, scratch=Path("/tmp"),
                            resume=None)
    outcome = adapter.run(replace(command, on_line=on_line))
    return outcome, adapter.parse(outcome.stdout, outcome.stderr_tail, outcome.exit_code, NOW)


def test_the_hash_covers_prompt_schema_and_access():
    base = call_sha256(params())
    assert len(base) == 64
    assert call_sha256(params(run="R-20261008T000000Z-assess", limits=Limits(1, 1, 1, 1, 1))) == base
    assert call_sha256(params(prompt="另一个提示")) != base
    assert call_sha256(params(schema={"type": "array"})) != base
    assert call_sha256(params(access=Access.WRITE)) != base


def test_a_recorded_call_goes_through_the_tool_parser(tmp_path):
    record(tmp_path, params())
    seen: list[str] = []
    adapter = replay(tmp_path)
    outcome, parsed = play(adapter, params(), lambda line: seen.append(line))
    assert (outcome.exit_code, outcome.stopped_by, len(seen)) == (0, None, 6)
    assert (parsed.status, parsed.structured, parsed.session_id) == (
        CallStatus.OK, {"verdict": "confirmed"}, "5f0c2a1e-8d3b-4c6e-9a41-7b2d9e0f1c3a")
    assert adapter.parse_line(seen[1]).tool_calls == 1  # 逐行兜底按录制时的工具计数


def test_calls_are_numbered_per_point_subject_and_round(tmp_path):
    record(tmp_path, params(), 1)
    record(tmp_path, params(), 2, stdout='{"type":"result","subtype":"success","is_error":false,"result":"{}"}')
    adapter = replay(tmp_path)
    assert play(adapter, params())[1].structured == {"verdict": "confirmed"}
    assert play(adapter, params())[1].text == "{}"
    _, third = play(adapter, params())
    assert third.status is CallStatus.FAILED and "replay-missing" in third.error


def test_missing_and_changed_recordings(tmp_path):
    record(tmp_path, params())
    _, missing = play(replay(tmp_path), params(subject="P-0043"))
    assert missing.status is CallStatus.FAILED and "replay-missing" in missing.error
    _, changed = play(replay(tmp_path), params(prompt="改过的提示"))
    assert changed.status is CallStatus.FAILED and "replay-task-changed" in changed.error


def test_the_line_callback_can_stop_a_replay(tmp_path):
    record(tmp_path, params())
    outcome, _ = play(replay(tmp_path), params(), lambda line: "turns" if '"tool_use"' in line else None)
    assert (outcome.exit_code, outcome.stopped_by, outcome.stdout.count("\n")) == (None, "turns", 1)


def test_patches_are_applied_to_the_workdir_on_the_first_call(tmp_path):
    writer = params(workdir=tmp_path / "wt", access=Access.WRITE, subject="0018", point="implement.code")
    record(tmp_path / "set", writer, patch="diff --git a/README.md b/README.md\n")
    runner = FakeRunner()
    outcome, _ = play(replay(tmp_path / "set", runner), writer)
    assert outcome.exit_code == 0
    (apply,) = runner.commands
    assert apply.argv == ("git", "apply", "--whitespace=nowarn", str(tmp_path / "set" / "implement.code-0018.1" /
                                                                       "changes.patch"))
    assert apply.cwd == tmp_path / "wt"
    failing = FakeRunner(exit_code=1)
    outcome, parsed = play(replay(tmp_path / "set", failing), writer)
    assert (outcome.exit_code, parsed.status) == (1, CallStatus.FAILED)
    assert "录制的改动无法应用" in outcome.stderr_tail and "patch does not apply" in outcome.stderr_tail


def test_a_later_call_with_its_own_patch_applies_it(tmp_path):
    """同一 Issue 回归后又一次修复：编码是同一(调用点、对象、轮次)的第 2 次调用，带它自己录制的改动；没带的不打。"""
    writer = params(workdir=tmp_path / "wt", access=Access.WRITE, subject="0018", point="implement.code", round=1)
    record(tmp_path / "set", writer, 1)
    record(tmp_path / "set", writer, 2, patch="diff --git a/README.md b/README.md\n")
    runner = FakeRunner()
    adapter = replay(tmp_path / "set", runner)
    play(adapter, writer)
    assert runner.commands == []
    play(adapter, writer)
    (apply,) = runner.commands
    assert apply.argv[-1] == str(tmp_path / "set" / "implement.code-0018.2" / "changes.patch")


def test_invalid_recording_sets_are_rejected(tmp_path):
    with pytest.raises(CallConfigError):
        replay(tmp_path)
    (tmp_path / "index.json").write_text(json.dumps({"recordings": [{"point": "assess.triage"}]}), encoding="utf-8")
    with pytest.raises(CallConfigError, match="缺少"):
        replay(tmp_path)
    entry = index_entry(params(), 1, "nowhere", "claude")
    (tmp_path / "index.json").write_text(json.dumps({"recordings": [entry]}), encoding="utf-8")
    with pytest.raises(CallConfigError, match="stdout.jsonl"):
        replay(tmp_path)
