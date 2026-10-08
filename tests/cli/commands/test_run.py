import pytest

from tightrein.cli import exit_codes
from tightrein.cli.text import text
from tightrein.protocol import schedule
from tightrein.protocol.schedule import Outcome, RunStatus, Step, StepStatus


def fixed(outcome_of):
    calls = []

    def run(runtime, **options):
        calls.append(options)
        return outcome_of(runtime)

    run.calls = calls
    return run


@pytest.mark.parametrize("make, code", [
    (lambda run: Outcome(run, RunStatus.DONE, steps=[Step("retro", None, StepStatus.PASSED, "复盘完成")]),
     exit_codes.OK),
    (lambda run: Outcome(run, RunStatus.FAILED, steps=[Step("collect", None, StepStatus.FAILED, "坏了")]),
     exit_codes.FAILED),
    (lambda run: Outcome(run, RunStatus.DONE, steps=[Step("implement", "0019", StepStatus.PENDING, "方案需确认")]),
     exit_codes.GATE),
    (lambda run: Outcome(run, RunStatus.REFUSED, "处于暂停"), exit_codes.REFUSED),
    (lambda run: Outcome(run, RunStatus.SKIPPED, "不在能跑的时段"), exit_codes.OK),
])
def test_the_exit_code_follows_the_outcome(cli, monkeypatch, make, code):
    monkeypatch.setattr(schedule, "run", fixed(lambda runtime: make(runtime.run)))
    data = cli.json("run")
    assert data["exitCode"] == code
    assert data["result"]["run"].startswith("R-20261008T030000Z-")


def test_a_gate_points_to_the_pending_document_and_a_failure_to_the_failure_document(cli, monkeypatch):
    monkeypatch.setattr(schedule, "run", fixed(lambda runtime: Outcome(runtime.run, RunStatus.DONE, steps=[
        Step("implement", "0019", StepStatus.PENDING, "方案需确认")])))
    assert cli.json("run")["next"] == "tightrein show 0019 --doc pending"
    monkeypatch.setattr(schedule, "run", fixed(lambda runtime: Outcome(runtime.run, RunStatus.FAILED, steps=[
        Step("release", "0020", StepStatus.FAILED, "CI 不通过")])))
    assert cli.json("run")["next"] == "tightrein show 0020 --doc failure"


def test_stage_object_trigger_and_dry_run_are_passed_to_the_schedule(cli, monkeypatch):
    fake = fixed(lambda runtime: Outcome(runtime.run, RunStatus.DONE))
    monkeypatch.setattr(schedule, "run", fake)
    cli("run", "implement", "--object", "19", "--dry-run")
    cli("run", "--trigger", "schedule")
    assert [(item["stage"], item["subject"], item["dry_run"], item["trigger"]) for item in fake.calls] == [
        ("implement", "0019", True, "manual"), (None, None, False, "schedule")]
    assert cli("run", "deploy").code == exit_codes.USAGE
    assert cli("run", "--object", "#abc").code == exit_codes.USAGE


def test_debug_keeps_the_prompt_and_raw_of_successful_calls(cli, monkeypatch, capsys):
    """--debug 设 AgentContext.debug：成功的模型调用也保存 prompt 与 raw(重新录制整体测试时用)。"""
    seen = []
    monkeypatch.setattr(schedule, "run", fixed(lambda runtime: seen.append(runtime.agents.debug)
                                               or Outcome(runtime.run, RunStatus.DONE)))
    cli("run")
    cli("run", "--debug")
    assert seen == [False, True]
    capsys.readouterr()
    cli("run", "--help")
    assert text("zh", "help.run_debug") in capsys.readouterr().out and text("en", "help.run_debug")
