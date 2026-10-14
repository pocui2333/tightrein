import json
from datetime import UTC, datetime, timedelta
from typing import Any

from tightrein.cli.render.snapshot import point_of, stage_steps, status_snapshot, watch_snapshot
from tightrein.protocol import recovery
from tightrein.protocol.handoff import Metrics, Status, Tokens
from tightrein.protocol.naming import FileName, format_iso
from tightrein.store.files.json import write_json
from tightrein.store.tables import counters, issues, runs
from tightrein.store.tables.issues import Issue
from tightrein.store.tables.runs import Run


def test_status_reads_waiting_running_and_queued_objects(world: Any):
    snapshot = status_snapshot(world.source)
    assert [(item.id, item.kind, item.command) for item in snapshot.waiting] == [
        ("0022", "failed", "tightrein show 0022"), ("0019", "review", "tightrein approve 0019")]
    assert snapshot.waiting[1].document == "data/issues/0019/90-issue-pending.md"
    assert snapshot.waiting[1].point == "implement.approve"
    assert snapshot.waiting[0].summary == "被安全分类拒绝，备用模型也拒绝"
    active = snapshot.active[0]
    assert (active.id, active.point, active.round) == ("0023", "implement.code", 2)
    assert (active.tokens_used, active.tokens_limit) == (1_200_000, 2_000_000)
    assert (active.files, active.added, active.deleted) == (4, 48, 12)
    assert active.step_limit_s == 30 * 60
    assert snapshot.stock.queued == ("0024", "0025")


def test_status_reads_system_quota_and_health(world: Any):
    snapshot = status_snapshot(world.source)
    assert snapshot.commit == "8f3a9e1"
    assert snapshot.setup_missing == ()
    assert snapshot.control.mode == "normal"
    assert snapshot.in_window
    assert snapshot.next_run == datetime(2026, 10, 7, 3, 30, tzinfo=UTC)
    assert snapshot.current is not None and snapshot.current.id == world.run and snapshot.current.status == "running"
    assert snapshot.last is not None and snapshot.last.stage == "collect" and snapshot.last.status == "done"
    claude, agy = snapshot.quotas
    assert (claude.tool, claude.five_hour, claude.weekly) == ("claude", 0.62, 0.41)
    assert claude.five_hour_resets_at == world.now + timedelta(hours=1, minutes=18)
    assert (agy.tool, agy.five_hour) == ("agy", 0.34)
    assert snapshot.reserve_reasons == ()
    assert snapshot.health.blind_spots == ("collect.alerts", "collect.api_fuzz")
    assert snapshot.stock.sources_enabled == 5 and snapshot.stock.sources_total == 7


def test_status_counts_stock_and_totals(world: Any):
    snapshot = status_snapshot(world.source)
    stock = snapshot.stock
    assert stock.problems == {"new": 14, "watching": 4, "muted": 3, "regressed": 2}
    assert stock.assess_pending == 16
    assert sum(stock.verdicts.values()) == 23
    assert sum(stock.severities.values()) == 23
    today = snapshot.today
    assert today.failed == 2  # 0022 的方案、0023 的审查第 1 轮
    assert (today.approvals_auto, today.approvals_manual) == (1, 0)
    assert today.total_tokens == 509_000
    assert today.cache_hit == 380_000 / 500_000
    assert today.problems == 23 and today.issues == 5
    assert snapshot.week.total_tokens == today.total_tokens


def test_a_failure_document_older_than_the_last_step_is_no_longer_waiting(world: Any):
    world.handoff("0022", "implement.design", round_=2, minutes_ago=1)
    assert [item.id for item in status_snapshot(world.source).waiting] == ["0019"]


def test_runs_whose_process_is_gone_show_as_interrupted(world: Any):
    world.alive.clear()
    snapshot = status_snapshot(world.source)
    assert snapshot.current is not None
    assert (snapshot.current.id, snapshot.current.status, snapshot.current.gone_reason) == (
        world.run, "interrupted", "process")
    assert snapshot.health.stale_runs == (world.run,)
    assert watch_snapshot(world.source).run.status == "interrupted"


def test_a_stale_heartbeat_is_interrupted_even_when_the_process_lives_elsewhere(world: Any):
    runs.start(world.source.conn, Run("R-20261007T020000Z-collect", "collect", "manual", "running",
                                      world.now - timedelta(hours=1), heartbeat_at=world.now - timedelta(minutes=5),
                                      holder_pid=1, holder_host="other-host"))
    assert "R-20261007T020000Z-collect" in status_snapshot(world.source).health.stale_runs


def test_pause_is_shown_on_the_running_run(world: Any):
    recovery.pause(world.layout, world.clock, note="看一下")
    snapshot = status_snapshot(world.source)
    assert (snapshot.control.mode, snapshot.control.note) == ("paused", "看一下")
    assert snapshot.current is not None and snapshot.current.status == "paused"


def test_open_breakers_are_listed_until_the_pause_ends(world: Any):
    conn, clock = world.source.conn, world.clock
    counters.add(conn, "breaker.dependency.collect.platform_errors.failures", 5, clock)
    counters.add(conn, "breaker.dependency.collect.platform_errors.opened", world.now.timestamp() - 20, clock)
    breakers = status_snapshot(world.source).health.breakers
    assert [(item.dependency, item.failures) for item in breakers] == [("collect.platform_errors", 5)]
    assert breakers[0].reopens_at == world.now + timedelta(seconds=40)
    world.clock.advance(timedelta(minutes=2))
    assert status_snapshot(world.source).health.breakers == ()


def test_watch_lists_every_issue_in_progress_with_its_steps_and_call(world: Any):
    snapshot = watch_snapshot(world.source)
    assert snapshot.run is not None and snapshot.run.id == world.run
    assert (snapshot.project, snapshot.stage) == ("ai-interview-collector", "implement")
    assert [row.id for row in snapshot.active] == ["0023"]  # 0019、0022 等你处理，列在那边
    row = snapshot.active[0]
    states = {mark.point: (mark.state, mark.note) for mark in row.steps}
    assert states["implement.approve"] == ("done", "auto")
    assert states["implement.code"][0] == "active"
    assert states["implement.check"][0] == "waiting"  # 还停在第 1 轮：这一轮还没做
    assert (row.point, row.step, row.round, row.action) == ("implement.code", "implement.code", 2, "model")
    assert row.call is not None and (row.call.tool, row.call.model, row.call.effort) == ("claude", "opus", "high")
    assert row.step_started == world.now - timedelta(minutes=8) and row.step_limit_s == 30 * 60
    assert (row.tokens_used, row.tokens_limit) == (1_200_000, 2_000_000)
    assert not row.stalled and row.last_failure is None
    assert [(item.id, item.kind, item.command) for item in snapshot.waiting] == [
        ("0022", "failed", "tightrein show 0022"), ("0019", "review", "tightrein approve 0019")]
    assert snapshot.manual == ()
    assert [quota.tool for quota in snapshot.quotas] == ["claude", "agy"]
    queue = snapshot.queue
    assert (queue.todo, queue.assess_pending, queue.ci, queue.accepting, queue.retro_after) == (
        ("0024", "0025"), 16, 0, 0, False)
    assert [event.point for event in snapshot.events][:2] == ["implement.code", "implement.review"]
    assert snapshot.events[0].round == 2 and snapshot.events[1].mark == "bad"
    assert snapshot.events[-1].mark == "warn" and snapshot.events[-1].subject is None


def test_parallel_sessions_are_all_listed_with_their_live_calls(world: Any):
    for identifier, point in (("0030", "implement.design"), ("0031", "implement.review.deep")):
        issues.save(world.source.conn, Issue(identifier, "implementing", f"并行 {identifier}", "bug", "problem",
                                             stage="implement", step=point.split(".")[1],
                                             extra={"github": {"number": 41}}), world.clock)
        write_json(world.layout.step_file(identifier, FileName(point, "started", "json")),
                   {"point": point, "subject": identifier, "run": world.run, "tool": "claude", "model": "sonnet",
                    "startedAt": format_iso(world.now - timedelta(minutes=2)), "endedAt": None, "turns": 7,
                    "tokens": {"input": 372_027, "output": 107, "cacheRead": 307_193, "cacheWrite": 64_822}})
    rows = {row.id: row for row in watch_snapshot(world.source).active}
    assert list(rows) == ["0023", "0030", "0031"]
    deep = rows["0031"]
    assert (deep.github, deep.point, deep.step) == (41, "implement.review.deep", "implement.review")
    assert deep.call is not None and deep.call.turns == 7
    assert deep.call.tokens == Tokens(372_027, 107, 307_193, 64_822)


def test_watch_shows_why_the_previous_call_of_this_step_ended(world: Any):
    marker = world.layout.step_file("0023", FileName("implement.code", "started", "json", round=1))
    write_json(marker, {"point": "implement.code", "subject": "0023", "run": world.run, "tool": "claude",
                        "model": "opus", "effort": "high", "startedAt": format_iso(world.now - timedelta(minutes=25)),
                        "endedAt": format_iso(world.now - timedelta(minutes=10)), "status": "timeout",
                        "durationMs": 1})
    assert watch_snapshot(world.source).active[0].last_failure == "timeout"


def test_an_issue_is_stalled_when_review_returns_the_same_blockers_again(world: Any):
    world.handoff("0023", "implement.code", round_=2, minutes_ago=5, facts={"diffHash": "a2"},
                  metrics=Metrics(duration_ms=1000, files_changed=4, lines_changed=60))
    world.handoff("0023", "implement.review", round_=2, status=Status.FAILED, minutes_ago=4,
                  facts={"blockers": [{"location": "src/api.ts:31", "kind": "type", "summary": "缺参数"}]})
    assert not watch_snapshot(world.source).active[0].stalled  # 阻断项变了、diff 也变了：有进展
    world.handoff("0023", "implement.code", round_=3, minutes_ago=3, facts={"diffHash": "a3"})
    world.handoff("0023", "implement.review", round_=3, status=Status.FAILED, minutes_ago=2,
                  facts={"blockers": [{"location": "src/api.ts:31", "kind": "type", "summary": "还是缺参数"}]})
    assert watch_snapshot(world.source).active[0].stalled


def test_problems_being_assessed_and_problems_handed_to_you(world: Any):
    path = world.layout.events(world.run)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": format_iso(world.now), "run": world.run, "subject": "P-0003",
                                 "point": "assess.triage", "kind": "action", "summary": "开始", "refs": {}}) + "\n")
    for point, started, ended in (("assess.dedup", 3, 2), ("assess.refute", 1, None)):
        marker = world.layout.step_file("P-0003", FileName(point, "started", "json", round=1, sequence=20))
        write_json(marker, {"point": point, "subject": "P-0003", "model": "opus",
                            "startedAt": format_iso(world.now - timedelta(minutes=started)),
                            "endedAt": None if ended is None else format_iso(world.now - timedelta(minutes=ended)),
                            "status": None if ended is None else "ok"})
    world.source.conn.execute("UPDATE problems SET extra = json_set(extra, '$.assess', json('{\"manual\": true}')) "
                              "WHERE id = 'P-0002'")
    snapshot = watch_snapshot(world.source)
    assert snapshot.stage == "assess"
    problem = snapshot.active[-1]
    assert (problem.id, problem.kind, problem.step, problem.title) == ("P-0003", "problem", "assess.confirm", "问题 3")
    assert [(mark.point, mark.state) for mark in problem.steps] == [
        ("assess.dedup", "done"), ("assess.triage", "waiting"), ("assess.confirm", "active"),
        ("assess.issue", "waiting")]
    assert snapshot.manual == ("P-0002",)
    assert snapshot.queue.assess_pending == 15


def test_the_collect_card_reads_each_source_of_the_collect_run(world: Any):
    collect = "R-20261007T031500Z-collect"
    runs.start(world.source.conn, Run(collect, "collect", "schedule", "running", world.now - timedelta(minutes=4),
                                      heartbeat_at=world.now, holder_pid=next(iter(world.alive)),
                                      holder_host=world.host))
    runs.finish(world.source.conn, world.run, "done", world.clock)
    world.handoff(collect, "collect.project_probes", facts={"read": 3},
                  metrics=Metrics(duration_ms=12_000, produced={"signals": 1}))
    world.handoff(collect, "collect.static", facts={"skipped": "无新提交"})
    world.handoff(collect, "collect.dedup", facts={"new": ["P-0100"], "merged": 4, "muted": 18, "regressed": 1})
    events = world.layout.events(collect)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(json.dumps({"at": "2026-10-07T03:16:00Z", "run": collect, "subject": None,
                                  "point": "collect.platform_errors.fetch", "kind": "action", "summary": "开始",
                                  "refs": {}}, ensure_ascii=False) + "\n", encoding="utf-8")
    snapshot = watch_snapshot(world.source)
    card = snapshot.collect
    marks = {mark.key: mark for mark in card.sources}
    assert (marks["collect.project_probes"].state, marks["collect.project_probes"].read,
            marks["collect.project_probes"].produced) == ("done", 3, 1)
    assert marks["collect.static"].state == "skipped" and marks["collect.static"].reason == "无新提交"
    assert marks["collect.alerts"].state == "off"
    assert marks["collect.platform_errors"].state == "active"  # 子步骤的事件记在它所在的来源
    assert marks["collect.access_log"].state == "waiting"
    assert card.dedup == (1, 4, 18, 1)
    assert card.started_at == datetime(2026, 10, 7, 3, 16, tzinfo=UTC)
    row = snapshot.active[0]
    assert (row.kind, row.id, row.step) == ("collect", "collect", "collect.platform_errors")
    assert "collect.alerts" not in [mark.point for mark in row.steps]  # 不启用的来源不占格
    assert row.steps[-1].point == "collect.dedup" and row.steps[-1].state == "done"


def test_reading_writes_nothing(world: Any):
    conn = world.source.conn
    before = conn.total_changes
    files = sorted(path for path in world.layout.root.rglob("*") if path.is_file())
    status_snapshot(world.source)
    watch_snapshot(world.source)
    assert conn.total_changes == before
    assert sorted(path for path in world.layout.root.rglob("*") if path.is_file()) == files


def test_point_of_and_stage_steps():
    assert point_of("implement", "code") == "implement.code"
    assert point_of("implement", "implement.check.runtime") == "implement.check.runtime"
    assert point_of("assess", None) == "assess"
    assert stage_steps("implement")[0] == "implement.prepare" and stage_steps("implement")[-1] == "implement.deliver"
    assert "collect.dedup" in stage_steps("collect")


def test_today_counts_only_handoffs_since_local_midnight(world: Any):
    world.handoff("0023", "implement.locate", status=Status.FAILED, minutes_ago=60 * 24)
    snapshot = status_snapshot(world.source)
    assert (snapshot.today.failed, snapshot.week.failed) == (2, 3)
