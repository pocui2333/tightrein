"""watch 的快照、界面与命令行。"""

import io
import json
import os
from datetime import timedelta, timezone

from cli_world import NOW, ZONE, make_cli_world
from rich.console import Console

from tightrein.cli import exit_codes
from tightrein.cli.main import main
from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import RunStage, RunStatus
from tightrein.domain.run import Run
from tightrein.monitor import snapshot, view
from tightrein.store.files import handoff_files
from tightrein.store.repos import runs

TRACE = "0123456789abcdef0123456789abcdef"
AT = parse_iso(NOW).astimezone(timezone.utc)
LOOP_ID, FIX_ID = "R-20261005-025000-loop", "R-20261005-025500-fix"


def event(at, operation, **fields):
    return {"timestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "run_id": fields.pop("run_id", None), "trace_id": TRACE,
            "span_id": f"{abs(hash((at, operation, str(fields)))) % 16 ** 16:016x}".replace("0" * 16, "1" * 16),
            "parent_span_id": None, "stage": fields.pop("stage", None), "operation": operation, **fields}


def seeded(tmp_path):
    """loop 运行开始于 10 分钟前：recovery、deployments 已执行，on-deploy 跳过，triage 正在执行；修复运行中
    fix-planner 已结束、fix-executor 正在调用。"""
    world = make_cli_world(tmp_path)
    app = world.app()
    runs.save(app.conn, Run(LOOP_ID, RunStage.LOOP, AT - timedelta(minutes=10), RunStatus.RUNNING))
    runs.save(app.conn, Run(FIX_ID, RunStage.FIX, AT - timedelta(minutes=5), RunStatus.RUNNING))
    gates = [("recovery", "执行", 10), ("deployments", "执行", 10), ("on-deploy", "跳过", 9), ("triage", "执行", 9)]
    lines = [event(AT - timedelta(minutes=minutes), "gate", run_id=LOOP_ID, stage="loop", decision=decision,
                   reason="理由", attributes={"step": step}) for step, decision, minutes in gates]
    lines.append(event(AT - timedelta(minutes=8), "invoke_agent", agent="claude", model="opus", cost_usd=0.5,
                       input_tokens=12000, output_tokens=345,
                       duration_ms=60000, status="ok", attributes={"role": "claim-verifier", "subjectId": "P-1"}))
    lines.append(event(AT - timedelta(minutes=7), "gate", stage="triage", decision="create-issue",
                       reason="判定为条件成立；其余"))
    log = app.layout.events_log(AT.date())
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    raw = app.layout.run_dir(FIX_ID) / "raw" / "runner"
    for role, finished in (("fix-planner", True), ("fix-executor", False)):
        directory = raw / f"{role}-0006"
        directory.mkdir(parents=True)
        marker = {"role": role, "subject": "0006", "tool": "claude", "model": "opus", "effort": "high",
                  "startedAt": (AT - timedelta(seconds=48)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        (directory / "started.json").write_text(json.dumps(marker), encoding="utf-8")
        if finished:
            (directory / "result.json").write_text("{}", encoding="utf-8")
            os.utime(directory / "started.json", (1, 1))
    return world, app


def test_snapshot_reads_steps_costs_agents_and_events(tmp_path):
    _, app = seeded(tmp_path)
    fix_dir = app.layout.fixes_dir("0006")
    fix_dir.mkdir(parents=True)
    (fix_dir / "pr-body.md").write_text("PR 正文\n", encoding="utf-8")
    taken = snapshot.take(app.conn, app.layout, "demo", AT, ZONE)
    assert taken.documents == () and taken.problems == []
    states = {step.name: (step.state, step.duration) for step in taken.steps}
    assert states["deployments"] == ("done", timedelta(minutes=1))
    assert states["on-deploy"][0] == "skipped" and states["triage"] == ("running", timedelta(minutes=9))
    assert states["health"] == ("waiting", None)
    assert taken.running and taken.run_tokens == 12345 and taken.day_tokens == 12345 and taken.pid is None
    assert [(agent.role, agent.subject) for agent in taken.agents] == [("fix-executor", "0006")]
    assert [(line.role, line.result, line.note) for line in taken.events] == [
        ("triage", "create-issue", "判定为条件成立"), ("claim-verifier", "ok", "P-1")]


def test_a_finished_loop_shows_the_step_results_and_anomalies_of_its_summary(tmp_path):
    _, app = seeded(tmp_path)
    loop = Run(LOOP_ID, RunStage.LOOP, AT - timedelta(minutes=10), RunStatus.FAILED, ended_at=AT - timedelta(minutes=1))
    runs.save(app.conn, loop)
    steps = [{"order": 1, "name": "recovery", "executed": True, "reason": "没有中断的运行", "status": "ok", "durationMs": 5},
             {"order": 2, "name": "triage", "executed": True, "reason": "3 个问题", "status": "failed",
              "durationMs": 61000}]
    handoff_files.write(app.layout, {"schemaVersion": 2, "runId": LOOP_ID, "stage": "loop",
                                     "subject": {"type": "run", "id": LOOP_ID}, "status": "failed", "inputsRef": {},
                                     "outputs": {"steps": steps, "anomalies": [
                                         {"source": "triage", "reason": "ValueError: 分诊失败", "log": None}]},
                                     "nextAction": "x", "blockedReason": "triage 失败",
                                     "createdAt": "2026-10-05T02:59:00Z"}, app.clock, conn=app.conn)
    taken = snapshot.take(app.conn, app.layout, "demo", AT, ZONE)
    found = {step.name: step for step in taken.steps}
    assert (found["triage"].state, found["triage"].note) == ("failed", "ValueError: 分诊失败")
    assert found["triage"].duration == timedelta(minutes=8) and found["triage"].tokens == 12345
    assert found["triage"].model == "claude opus"
    assert found["recovery"].state == "done" and found["health"].state == "waiting" and not taken.running


def test_runs_whose_process_is_gone_show_as_interrupted(tmp_path):
    _, app = seeded(tmp_path)
    taken = snapshot.take(app.conn, app.layout, "demo", AT, ZONE, gone={LOOP_ID, FIX_ID})
    assert taken.loop.status is RunStatus.INTERRUPTED and not taken.running and taken.agents == ()
    assert {step.name: step.state for step in taken.steps}["triage"] == "interrupted"
    console = Console(width=140, record=True, file=io.StringIO())
    console.print(view.render(taken, 140, ZONE, 2))
    text = console.export_text()
    assert "中断" in text and "✗" not in text and "tightrein run(接管中断的运行并继续)" in text


def test_the_view_fits_wide_and_narrow_terminals(tmp_path):
    _, app = seeded(tmp_path)
    taken = snapshot.take(app.conn, app.layout, "demo", AT, ZONE)
    for width in (140, 80):
        console = Console(width=width, record=True, file=io.StringIO())
        console.print(view.render(taken, width, ZONE, 2))
        text = console.export_text()
        assert all(len(line) <= width for line in text.splitlines())
        assert "fix-executor" in text and "运行中" in text and "12.3k" in text and "建 Issue" in text
        assert len(text.splitlines()) == view.HEIGHT
    assert view.duration(timedelta(seconds=0.4)) == "0.4s" and view.duration(100) == "1分40秒"
    assert view.duration(timedelta(hours=2, minutes=5)) == "2时05分"


def test_a_continue_run_shows_the_fix_progress_attempts_and_last_failure():
    """continue 推进的运行只有状态恢复一步：脉络与明细改为进行中的 Issue 的修复步骤；正在调用的排在前面，
    上一次超时的调用写进备注。"""
    loop = Run(LOOP_ID, RunStage.LOOP, AT - timedelta(minutes=20), RunStatus.RUNNING)
    steps = (snapshot.StepState("recovery", "done", AT - timedelta(minutes=20), timedelta(seconds=1)),
             *(snapshot.StepState(name, "waiting") for name in snapshot.LOOP_STEPS[1:]))
    fix_steps = tuple(snapshot.FixStep(label, state) for label, state in (
        ("分流", "done"), ("准备", "done"), ("勘察", "pending"), ("出计划", "pending")))
    idle = snapshot.ActiveIssue("0015", "体检", "P2", None, "standard", fix_steps)
    busy = snapshot.ActiveIssue("0017", "登录", "P2", None, "standard", fix_steps, calls=(
        snapshot.FixCall("fix-scout", "agy gemini", "ok", 450000, 3_400_000),
        snapshot.FixCall("fix-scout", "agy gemini", "limit-reached", 906000, 656_000)))
    agent = snapshot.AgentCall("fix-scout", "0017", "agy", "gemini", None, AT - timedelta(minutes=3))
    taken = snapshot.Snapshot("demo", AT, loop, steps, 0, 0, (idle, busy), (agent,), (), ())
    console = Console(width=140, record=True, file=io.StringIO())
    console.print(view.render(taken, 140, ZONE, 2))
    text = console.export_text()
    assert "流程脉络: Issue 0017 已完成(2) ── 勘察 (当前) ── 等待(1)" in text
    lines = text.splitlines()
    first = next(index for index, line in enumerate(lines) if "1. Issue" in line)
    assert "Issue 0017 勘察" in lines[first] and "运行中" in lines[first] and "fix-scout 第 3 次" in lines[first]
    assert "上一次到达上限(15分06秒)，正在重试" in lines[first + 1]
    assert "2. Issue 0015 勘察" in text and len(lines) == view.HEIGHT


def test_watch_once_prints_a_frame_and_rejects_json(tmp_path):
    world, _ = seeded(tmp_path)
    out = io.StringIO()
    code = main(["watch", "--once", "--workspace", str(world.root)], world.externals(), stdin=io.StringIO(),
                stdout=out, stderr=io.StringIO())
    assert code == exit_codes.OK and "tightrein >>>" in out.getvalue() and "步骤明细" in out.getvalue()
    out = io.StringIO()
    code = main(["watch", "--json", "--workspace", str(world.root)], world.externals(), stdin=io.StringIO(),
                stdout=out, stderr=io.StringIO())
    assert code == exit_codes.USAGE and "不支持 --json" in out.getvalue()
