"""去重的整个流程：真实数据库与文件，git 与事件日志用替身。"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tightrein.collect.dedup import output
from tightrein.collect.dedup.dedup import dedup, recover
from tightrein.collect.dedup.status import IGNORE, RESOLVED_COMMIT, IgnoreCondition
from tightrein.protocol.handoff import read
from tightrein.protocol.naming import FileName
from tightrein.store.rebuild import rebuild
from tightrein.store.tables import issues, occurrences, problems, sequences, state
from tightrein.store.tables.issues import Issue
from tightrein.store.tables.problems import Problem

T = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
HANDOFF = FileName("collect.dedup", "handoff", "json")


def stored_problem(conn, clock, **fields):
    values = {"id": "P-0001", "fingerprint": "g1", "source": "collect.platform_errors", "check_type": "error",
              "status": "new", "title": "t", "first_seen": T, "last_seen": T, "last_commit": "c1", "extra": {}}
    values.update(fields)
    problems.save(conn, Problem(**values), clock)
    sequences.ensure_at_least(conn, sequences.PROBLEM, int(values["id"][2:]))
    return values["id"]


def next_run(runtime, clock, hours=1):
    clock.advance(timedelta(hours=hours))
    runtime.run = f"R-{clock.now().strftime('%Y%m%dT%H%M%SZ')}-collect"


def test_new_problems_are_written_in_one_go_with_handoffs(runtime, conn, layout, make_signal, make_result):
    signals = [make_signal(group_key="g1"), make_signal(group_key="g1", occurred_at="2026-10-07T11:10:00Z"),
               make_signal(source="collect.static", check_type="static", location="a.py:3", verified=True,
                           evidence={"rule": "swallow"})]
    results = [make_result("collect.platform_errors", signals[:2], state={"collect.platform_errors:sentry": {"u": 1}}),
               make_result("collect.static", signals[2:])]
    outcome = dedup(runtime, results)
    assert outcome.new == ["P-0001", "P-0002"] and outcome.regressed == []
    assert [item.count for item in problems.find(conn)] == [2, 1]
    assert len(occurrences.find(conn, "P-0001")) == 2
    assert state.get(conn, "collect.platform_errors:sentry") == {"u": 1}
    assert sequences.current(conn, sequences.PROBLEM) == 2
    assert state.get(conn, output.UNWRITTEN_KEY) is None
    run_handoff = read(layout.step_file(runtime.run, HANDOFF))
    assert run_handoff.facts["forAssess"] == ["P-0001", "P-0002"] and run_handoff.metrics.produced["signals"] == 3
    problem_handoff = read(layout.step_file("P-0001", HANDOFF))
    assert problem_handoff.facts["transition"]["after"] == "new"
    assert problem_handoff.facts["latest"]["occurredAt"] == "2026-10-07T11:10:00Z"
    assert layout.step_file(runtime.run, FileName("collect.dedup", "log", "jsonl")).is_file()


def test_results_are_independent_of_the_order_sources_hand_them_in(runtime, make_signal, make_result):
    early = make_signal(group_key="a", occurred_at="2026-10-07T08:00:00Z", deterministic=True)
    late = make_signal(group_key="b", occurred_at="2026-10-07T09:00:00Z", deterministic=True)
    outcome = dedup(runtime, [make_result("collect.alerts", [late]), make_result("collect.platform_errors", [early])])
    assert outcome.new == ["P-0001", "P-0002"]
    assert problems.get(runtime.conn, "P-0001").fingerprint == "a"


def test_project_suppression_rules_drop_signals(runtime, configure, make_signal, make_result):
    configure(suppress=[{"match": {"source": "collect.platform_errors", "messagePattern": "^noise"},
                         "reason": "误报", "addedOn": "2026-10-01", "expiresOn": "2026-12-31"}])
    outcome = dedup(runtime, [make_result("collect.platform_errors", [make_signal(message="noise 1", group_key="x"),
                                                                       make_signal(message="real", group_key="y")])])
    assert outcome.muted == 1 and problems.by_fingerprint(runtime.conn, "x") is None


def test_watching_problems_become_new_when_they_repeat_within_the_window(runtime, clock, make_signal, make_result):
    first = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g")])])
    assert first.new == [] and first.watching == 1
    next_run(runtime, clock)
    again = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g", run=runtime.run,
                                                                               occurred_at="2026-10-07T12:30:00Z")])])
    assert again.new == ["P-0001"] and problems.get(runtime.conn, "P-0001").status == "new"


def test_an_intermittent_problem_that_shows_up_again_becomes_new(runtime, conn, clock, make_signal, make_result):
    stored_problem(conn, clock, status="intermittent")
    outcome = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g1")])])
    assert outcome.new == ["P-0001"] and problems.get(conn, "P-0001").status == "new"


def test_a_muted_problem_comes_back_when_a_signal_raises_its_severity(runtime, conn, clock, make_signal, make_result):
    condition = IgnoreCondition(severity_escalated=True, baseline_occurrences=1).to_json()
    stored_problem(conn, clock, status="muted", extra={IGNORE: condition, "severity": "P2"})
    same = replace(make_signal(group_key="g1"), severity_hint="P2")
    assert dedup(runtime, [make_result("collect.platform_errors", [same])]).new == []
    assert problems.get(conn, "P-0001").status == "muted"
    higher = replace(make_signal(group_key="g1"), severity_hint="P1")
    outcome = dedup(runtime, [make_result("collect.platform_errors", [higher])])
    assert outcome.new == ["P-0001"] and problems.get(conn, "P-0001").status == "new"


def test_covered_runs_on_newer_commits_resolve_problems(runtime, clock, make_signal, make_result):
    signal = make_signal(source="collect.static", check_type="static", location="a.py:3", verified=True,
                         commit="c1", evidence={"rule": "swallow"})
    dedup(runtime, [make_result("collect.static", [signal], coverage=["a.py"])])
    next_run(runtime, clock)
    runtime.git.current = "c1"  # 同一个 commit：不算
    assert dedup(runtime, [make_result("collect.static", [], coverage=["a.py"])]).resolved == 0
    next_run(runtime, clock)
    runtime.git.current = "c2"
    outcome = dedup(runtime, [make_result("collect.static", [], coverage=["a.py"])])
    assert outcome.resolved == 1
    resolved = problems.get(runtime.conn, "P-0001")
    assert resolved.status == "resolved" and resolved.extra[RESOLVED_COMMIT] == "c2"


def test_failed_or_partial_coverage_does_not_resolve_what_was_not_read(runtime, conn, clock, make_result):
    stored_problem(conn, clock, source="collect.static", location="a.py", extra={})
    runtime.git.current = "c2"
    failed = make_result("collect.static", [], status="failed")
    assert dedup(runtime, [failed]).resolved == 0
    other_file = make_result("collect.static", [], coverage=["b.py"])
    assert dedup(runtime, [other_file]).resolved == 0
    assert problems.get(conn, "P-0001").status == "new"


def test_resolved_problems_regress_only_on_newer_commits(runtime, conn, clock, make_signal, make_result):
    stored_problem(conn, clock, status="resolved", extra={RESOLVED_COMMIT: "c2"})
    late = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g1", commit="c1")])])
    assert late.regressed == [] and problems.get(conn, "P-0001").status == "resolved"
    unknown = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g1", commit="zz")])])
    assert unknown.regressed == [] and any("未知" in note for note in unknown.notes)
    again = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g1", commit="c3")])])
    assert again.regressed == ["P-0001"]
    assert problems.get(conn, "P-0001").count == 4
    asked = runtime.git.queries
    assert len(asked) == len(set(asked))


def test_regression_reopens_the_issue_and_is_sent_with_its_number(runtime, conn, clock, layout, make_signal,
                                                                    make_result):
    issues.save(conn, Issue(id="0018", status="done", title="t", kind="bug", origin="problem",
                            extra={"closeReason": "fixed"}), clock)
    stored_problem(conn, clock, status="resolved", issue="0018", extra={RESOLVED_COMMIT: "c2"})
    outcome = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g1", commit="c3")])])
    assert outcome.regressed == ["P-0001"] and outcome.reopened == ["0018"]
    reopened = issues.get(conn, "0018")
    assert reopened.status == "todo" and "P-0001" in reopened.extra["history"][-1]["note"]
    assert read(layout.step_file("P-0001", HANDOFF)).facts["issue"] == "0018"


def test_a_failed_reproduce_check_is_marked_on_the_regressed_problem(runtime, conn, clock, layout, make_signal,
                                                                      make_result):
    stored_problem(conn, clock, status="resolved", extra={RESOLVED_COMMIT: "c2"})
    check = make_signal(source="collect.project_probes", check_type="probe", commit="c3",
                        evidence={"targetFingerprints": ["g1"]})
    outcome = dedup(runtime, [make_result("collect.project_probes", [check])])
    assert outcome.regressed == ["P-0001"] and outcome.new == []
    facts = read(layout.step_file("P-0001", HANDOFF)).facts
    assert facts["regressionCheck"] is True and facts["transition"]["context"]["regressionCheck"] is True


def test_aliases_in_the_database_match_and_merged_problems_are_skipped(runtime, conn, clock, make_signal,
                                                                        make_result):
    stored_problem(conn, clock, fingerprint="old", extra={"aliases": ["g9"]})
    stored_problem(conn, clock, id="P-0002", fingerprint="g9", status="closed", extra={"mergedInto": "P-0001"})
    outcome = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g9")])])
    assert outcome.new == [] and sequences.current(conn, sequences.PROBLEM) == 2
    assert (problems.get(conn, "P-0001").count, problems.get(conn, "P-0002").count) == (2, 1)


def test_issues_still_being_fixed_are_left_alone(runtime, conn, clock, make_signal, make_result):
    issues.save(conn, Issue(id="0018", status="implementing", title="t", kind="bug", origin="problem"), clock)
    stored_problem(conn, clock, status="resolved", issue="0018", extra={RESOLVED_COMMIT: "c2"})
    outcome = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g1", commit="c3")])])
    assert outcome.regressed == ["P-0001"] and outcome.reopened == []
    assert issues.get(conn, "0018").status == "implementing" and any("0018" in note for note in outcome.notes)


def test_problems_with_issues_are_not_sent_again_when_seen_again(runtime, conn, clock, make_signal, make_result):
    stored_problem(conn, clock, status="ongoing", issue="0018")
    outcome = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g1")])])
    assert outcome.new == [] and problems.get(conn, "P-0001").count == 2


def test_a_refuted_problem_is_judged_correct_after_clean_covered_runs(runtime, conn, clock, configure, make_signal,
                                                                       make_result):
    configure(falsePositiveCleanRuns=2, suppress=[{"match": {"fingerprint": "g2-old"}, "reason": "判为误报",
                                                   "addedOn": "2026-10-01", "expiresOn": "2026-12-31"}])
    refuted = {"verdict": "refuted", "assess": {"attempt": 1, "verdict": "refuted", "outcome": None},
               "subSource": "sentry"}
    stored_problem(conn, clock, status="closed", extra=refuted)
    stored_problem(conn, clock, id="P-0002", fingerprint="g2", status="closed", extra={**refuted, "aliases": ["g2-old"]})
    stored_problem(conn, clock, id="P-0003", fingerprint="g3", status="closed", extra={**refuted, "subSource": "loki"})
    # 别名相同的信号被抑制了，不关联到问题，仍按指纹算「又出现过」
    first = dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g2-old")],
                                        coverage=["sentry"])])
    assert first.muted == 1 and not any("判对" in note for note in first.notes)
    next_run(runtime, clock)
    second = dedup(runtime, [make_result("collect.platform_errors", [], coverage=["sentry"])])
    assert [note for note in second.notes if "判对" in note] == ["P-0001：判为不成立后多次覆盖未再出现，评估结论回填为判对"]
    assert problems.get(conn, "P-0001").extra["assess"]["outcome"] == "correct"
    seen = problems.get(conn, "P-0002")
    assert seen.extra["assess"]["outcome"] is None and seen.extra["falsePositiveCheck"]["seen"] is True
    assert "falsePositiveCheck" not in problems.get(conn, "P-0003").extra  # 没覆盖到它所在的子来源
    next_run(runtime, clock)
    third = dedup(runtime, [make_result("collect.platform_errors", [], coverage=["sentry"])])
    assert not any("判对" in note for note in third.notes)  # 已有结果的不再回填


def test_a_failure_writes_nothing_and_keeps_the_read_position(runtime, conn, make_signal, make_result):
    broken = make_result("collect.platform_errors", [make_signal(group_key="g1", deterministic=True)],
                         state={"collect.platform_errors:sentry": {"bad": object()}})
    with pytest.raises(TypeError):
        dedup(runtime, [broken])
    assert problems.find(conn) == [] and state.get(conn, "collect.platform_errors:sentry") is None
    assert sequences.current(conn, sequences.PROBLEM) == 0


def test_interrupted_runs_get_their_files_written_next_time(runtime, conn, clock, layout):
    target = layout.step_file("R-20261007T110000Z-collect", HANDOFF)
    output.remember(conn, "R-20261007T110000Z-collect", {str(target.relative_to(layout.root)): "{}\n"}, clock)
    assert recover(runtime) == "R-20261007T110000Z-collect"
    assert json.loads(target.read_text(encoding="utf-8")) == {} and state.get(conn, output.UNWRITTEN_KEY) is None
    assert recover(runtime) is None


def test_rebuild_matches_incremental_dedup_and_keeps_every_id(runtime, conn, clock, layout, make_signal,
                                                              make_result):
    dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g1"), make_signal(group_key="g2")])])
    next_run(runtime, clock)
    dedup(runtime, [make_result("collect.platform_errors", [make_signal(group_key="g1", run=runtime.run,
                                                                        occurred_at="2026-10-07T12:30:00Z")])])
    before = [(item.id, item.status, item.count, item.fingerprint) for item in problems.find(conn)]
    seen_before = len(occurrences.find(conn, "P-0001"))
    rebuild(layout, conn)
    assert [(item.id, item.status, item.count, item.fingerprint) for item in problems.find(conn)] == before
    assert len(occurrences.find(conn, "P-0001")) == seen_before == 2
    assert sequences.current(conn, sequences.PROBLEM) == 2
