import json
from datetime import UTC, datetime, timedelta
from typing import Any

from tightrein.cli.render.snapshot import point_of, stage_steps, status_snapshot, watch_snapshot
from tightrein.protocol import recovery
from tightrein.protocol.handoff import Metrics, Status
from tightrein.protocol.naming import FileName, format_iso
from tightrein.store.files.json import write_json
from tightrein.store.tables import counters, runs
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


def test_watch_shows_the_steps_the_call_and_the_last_review(world: Any):
    snapshot = watch_snapshot(world.source)
    assert snapshot.run is not None and snapshot.run.id == world.run
    assert (snapshot.tool, snapshot.model, snapshot.effort) == ("claude", "opus", "high")
    card = snapshot.subject
    assert card is not None and card.id == "0023"
    states = {mark.point: (mark.state, mark.note) for mark in card.steps}
    assert states["implement.approve"] == ("done", "auto")
    assert states["implement.code"][0] == "active"
    assert states["implement.check"][0] == "waiting"  # 还停在第 1 轮：这一轮还没做
    assert (card.point, card.round, card.action) == ("implement.code", 2, "model")
    assert card.review is not None and not card.review.passed
    assert card.review.blockers[0] == ("src/notes.ts:88", "未处理保存失败")
    assert card.progress is None
    assert card.gate_paths == ("package.json",)
    assert (card.returned, card.files, card.files_limit, card.lines_limit) == (1, 4, 10, 400)
    assert card.spent_limit_s == 2 * 3600
    assert snapshot.next_point == "implement.check"
    assert snapshot.queued == ("0024", "0025")
    assert [event.point for event in snapshot.events][:2] == ["implement.code", "implement.review"]
    assert snapshot.events[0].round == 2 and snapshot.events[1].mark == "bad"
    assert snapshot.events[-1].mark == "warn"


def test_watch_shows_why_the_previous_call_of_this_step_ended(world: Any):
    marker = world.layout.step_file("0023", FileName("implement.code", "started", "json", round=1))
    write_json(marker, {"point": "implement.code", "subject": "0023", "run": world.run, "tool": "claude",
                        "model": "opus", "effort": "high", "startedAt": format_iso(world.now - timedelta(minutes=25)),
                        "endedAt": format_iso(world.now - timedelta(minutes=10)), "status": "timeout",
                        "durationMs": 1})
    card = watch_snapshot(world.source).subject
    assert card is not None and card.last_failure == "timeout"


def test_progress_compares_blockers_and_diff_with_the_previous_round(world: Any):
    world.handoff("0023", "implement.code", round_=2, minutes_ago=5, facts={"diffHash": "a2"},
                  metrics=Metrics(duration_ms=1000, files_changed=4, lines_changed=60))
    world.handoff("0023", "implement.review", round_=2, status=Status.FAILED, minutes_ago=4,
                  facts={"blockers": [{"location": "src/api.ts:31", "kind": "type", "summary": "缺参数"}]})
    card = watch_snapshot(world.source).subject
    assert card is not None
    assert (card.progress, card.diff_changed, card.blockers_changed) == (True, True, True)


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
                                  "point": "collect.platform_errors", "kind": "action", "summary": "开始",
                                  "refs": {}}, ensure_ascii=False) + "\n", encoding="utf-8")
    card = watch_snapshot(world.source).collect
    marks = {mark.key: mark for mark in card.sources}
    assert (marks["collect.project_probes"].state, marks["collect.project_probes"].read,
            marks["collect.project_probes"].produced) == ("done", 3, 1)
    assert marks["collect.static"].state == "skipped" and marks["collect.static"].reason == "无新提交"
    assert marks["collect.alerts"].state == "off"
    assert marks["collect.platform_errors"].state == "active"
    assert marks["collect.access_log"].state == "waiting"
    assert card.dedup == (1, 4, 18, 1)


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
