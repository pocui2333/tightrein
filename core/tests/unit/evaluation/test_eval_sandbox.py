import json

import pytest
from eval_world import TRIAGE_OUTPUTS

from tightrein.domain.enums import RunnerStatus
from tightrein.evaluation import rubric
from tightrein.evaluation.cases import load_module_case
from tightrein.evaluation.sandbox import SandboxRequest, SubprocessModuleRunner, collect
from tightrein.evaluation.variants import Variant, VersionSpec
from tightrein.runner.result import Usage

VARIANT = Variant("baseline", VersionSpec("baseline"), "claude", "claude-opus")


@pytest.fixture
def rig(world, tmp_path):
    snapshot = tmp_path / "snapshot"
    (snapshot / "workspaces" / "sample").mkdir(parents=True)
    directory = world.module_case("E-0001", input={"handoff": "handoff.json", "commit": "abc1234",
                                                   "args": ["--review-only"]})
    (directory / "gates.json").write_text("{}", encoding="utf-8")
    case, problems = load_module_case(world.layout, directory, rubric.item_ids)
    assert problems == []
    entry = world.fake_module(tmp_path / "module")

    def run(behavior, timeout=30.0, attempt=1, variant=VARIANT):
        world.behave(snapshot, {"E-0001": behavior})
        output = tmp_path / "outputs" / variant.label / "E-0001" / str(attempt)
        runner = SubprocessModuleRunner({"PATH": "/usr/bin:/bin"}, entry=entry, timeout_seconds=timeout)
        return runner.run(SandboxRequest(case, variant, snapshot, "sample", output, attempt)), output

    return run


def test_an_ok_run_is_collected_for_scoring(rig):
    outcome, output = rig({"outputs": TRIAGE_OUTPUTS, "cost": 0.25, "diff": "diff --git a/x b/x\n"})
    argv = json.loads((output / "argv.json").read_text(encoding="utf-8"))
    assert argv[:2] == ["triage", "--input"] and argv[2].endswith("evals/triage/E-0001/input/handoff.json")
    assert argv[3:] == ["--output", str(output), "--commit", "abc1234", "--ignore-state", "--runner", "claude",
                        "--model", "claude-opus", "--gate-decisions", argv[13], "--review-only", "--workspace",
                        str(output.parents[3] / "snapshot" / "workspaces" / "sample")]
    assert argv[13].endswith("evals/triage/E-0001/gates.json")
    assert (outcome.handoff["outputs"], outcome.status, outcome.failure()) == (TRIAGE_OUTPUTS, RunnerStatus.OK, None)
    assert outcome.usage == Usage(1000, 100, None, 0.25)
    assert outcome.transcripts == ("transcripts/claim-verifier-P-0042.jsonl",)
    assert (outcome.diff_text, outcome.stderr_path, outcome.unavailable) == ("diff --git a/x b/x\n", None, False)


@pytest.mark.parametrize("behavior, reason", [
    ({"status": "blocked", "outputs": TRIAGE_OUTPUTS}, "交接文档为 blocked：等待用户确认"),
    ({"outputs": TRIAGE_OUTPUTS, "runner": "limit-reached", "errorType": "turn-limit"}, "执行器结果为 limit-reached"),
    ({"outputs": TRIAGE_OUTPUTS, "runner": "schema-invalid"}, "执行器结果为 schema-invalid"),
    ({"outputs": {"verdict": "maybe"}}, "交接文档不合 schema：$.outputs"),
    ({"noHandoff": True, "runner": None}, "没有交接文档"),
])
def test_failed_runs_carry_the_reason(rig, behavior, reason):
    outcome, _ = rig(behavior)
    assert outcome.failure().startswith(reason)


def test_a_crash_keeps_stderr_and_a_timeout_is_terminated(rig):
    crashed, output = rig({"noHandoff": True, "exit": 3, "stderr": "Traceback: boom\n", "runner": None})
    assert (crashed.failure(), crashed.status) == ("子进程以 3 退出", RunnerStatus.FAILED)
    assert crashed.stderr_path == output / "stderr.log"
    assert crashed.stderr_path.read_text(encoding="utf-8") == "Traceback: boom\n"
    slow, _ = rig({"sleep": 10, "outputs": TRIAGE_OUTPUTS}, timeout=0.5, attempt=2)
    assert slow.failure() == "子进程超过 0.5 秒未结束，已终止"


def test_an_exit_code_does_not_hide_a_handoff(rig):
    outcome, _ = rig({"outputs": TRIAGE_OUTPUTS, "exit": 1})
    assert outcome.failure() is None


def test_an_unavailable_tool_is_reported(rig):
    outcome, _ = rig({"status": "failed", "runner": "failed", "errorType": "tool-unavailable"})
    assert outcome.unavailable is True


def test_more_than_one_handoff_is_a_run_error(tmp_path):
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    for name in ("triage-P-0001.json", "triage-P-0002.json"):
        (handoff / name).write_text("{}", encoding="utf-8")
    (handoff / "triage-P-0003.1.json").write_text("{}", encoding="utf-8")
    assert collect(tmp_path).failure() == "输出目录中的交接文档有 2 份，应恰好一份"
