"""冒烟测试：演示工作区上以 --runner replay 与假 git/gh 走通 collect 到 issue，并检查 next 与 continue。"""

from demo_world import NOW, DemoWorld

from tightrein.cli import exit_codes
from tightrein.domain.enums import RunStatus
from tightrein.domain.clock import SystemClock
from tightrein.store.files.layout import UserLayout
from tightrein.store.migrations.runner import open_database
from tightrein.store.repos import schedule_state

LATER = ["2026-10-05T09:05:00+09:00", "2026-10-05T09:10:00+09:00", "2026-10-05T09:15:00+09:00",
         "2026-10-05T09:20:00+09:00"]


def test_one_run_takes_an_archived_finding_to_an_issue_waiting_for_approval(tmp_path):
    world = DemoWorld(tmp_path)
    world.record([(("run",), NOW)])
    [(code, values)] = world.replayed([(("run",), NOW)])
    result = values["result"]
    assert code == 0 and result["status"] == RunStatus.BLOCKED.value
    assert result["produced"]["newProblems"] == ["P-0001"] and result["produced"]["issues"] == ["0001"]
    assert [(item["verdict"], item["disposition"]) for item in result["produced"]["triage"]] == [
        ("confirmed", "create-issue")]
    assert [(item["kind"], item["command"]) for item in result["waiting"]] == [
        ("issue-approval", "tightrein issue approve 1")]
    assert result["anomalies"] == []
    assert (world.root / result["report"]).is_file()
    conn = open_database(world.layout().database(), SystemClock())
    assert schedule_state.get(conn, "archive-import").last_status is RunStatus.OK
    conn.close()
    assert any("待处理 1 项" in call[2] and "tightrein：demo" in call[2] for call in world.notices.calls)
    code, values = world.call("status", now=LATER[0])
    assert code == 0 and values["result"]["waiting"][0]["subjectId"] == "0001"


def test_next_and_continue_follow_the_state_table(tmp_path):
    world = DemoWorld(tmp_path)
    steps = [(("collect", "--probe", "incidental", "--import-archive", str(world.archive)), NOW),
             (("aggregate", "--reproduce", "skip"), LATER[0]),
             (("continue", "P-0001", "--until", "triage"), LATER[1]),
             (("continue", "P-0001"), LATER[2])]
    world.record(steps)
    collected, aggregated = world.replayed(steps[:2])
    assert collected[0] == 0 and aggregated[0] == 0
    code, values = world.call("next", "P-0001", now=LATER[0])
    assert values["next"] == "tightrein triage --select P-0001" and values["result"][0]["canContinue"] is True
    [(code, values)] = world.replayed(steps[2:3])
    assert code == exit_codes.OK and values["result"]["stops"][0]["reason"] == "已到终点"
    assert values["next"] == "tightrein issue create --select P-0001"
    [(code, values)] = world.replayed(steps[3:])
    assert code == exit_codes.GATE and values["stoppedAt"]["gate"] == "issue-approval"
    assert values["next"] == "tightrein issue approve 1"
    code, values = world.call("next", "1", now=LATER[3])
    assert values["result"][0]["canContinue"] is False and values["result"][0]["gate"] == "issue-approval"
    config = UserLayout(world.home).config()
    config.parent.mkdir(parents=True)
    config.write_text("branchPrefix: demo\n", encoding="utf-8")
    code, values = world.call("issue", "approve", "1", now=LATER[3])
    assert code == exit_codes.GATE and values["pendingOperations"][0]["kind"] == "create-fix-worktree"
    code, values = world.call("next", "1", now=LATER[3])
    assert values["next"] == "tightrein fix start 1" and values["result"][0]["gate"] == "interactive-fix"
    code, values = world.call("continue", "1", now="2026-10-05T09:25:00+09:00")
    assert code == exit_codes.GATE and values["stoppedAt"]["gate"] == "interactive-fix"
