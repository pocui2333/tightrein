import json
from datetime import timedelta

import pytest

from tightrein.assess import persist
from tightrein.collect.dedup import suppress
from tightrein.collect.dedup.output import LOG, PROBLEM_RECORD, encode
from tightrein.store import rebuild
from tightrein.store.db import transaction
from tightrein.store.files.json import read_json
from tightrein.store.tables import problems
from tightrein.store.tables.problems import Problem

COLLECTED = "R-20261008T020000Z-collect"
EARLIER = "R-20261008T010000Z-assess"


def row(kit, problem_id: str, status: str, **extra) -> Problem:
    return Problem(id=problem_id, fingerprint=f"fp-{problem_id}", source="collect.platform_errors", check_type="error",
                   status=status, title="t", first_seen=kit.NOW - timedelta(days=1), last_seen=kit.NOW,
                   location="services/orders.py:3", extra=dict(extra))


def test_update_writes_the_verdict_and_severity_and_a_snapshot(runtime, make_problem, layout, kit):
    problem = make_problem()
    with transaction(runtime.conn):
        persist.update(runtime, problem, status="ongoing", issue="0007",
                       assess={"attempt": 1, "verdict": "confirmed", "severity": "P2"})
        persist.update(runtime, problems.get(runtime.conn, "P-0001"), assess={"issues": ["0007"]})
    stored = problems.get(runtime.conn, "P-0001")
    assert stored.status == "ongoing" and stored.issue == "0007"
    assert stored.extra["verdict"] == "confirmed" and stored.extra["severity"] == "P2"  # status、watch 读取
    assert stored.extra["assess"] == {"attempt": 1, "verdict": "confirmed", "severity": "P2", "issues": ["0007"]}
    path = persist.snapshot_path(layout, "P-0001")
    assert path.name == "00-problem-assess.json"
    data = read_json(path)
    assert data["run"] == kit.RUN and data["row"]["status"] == "ongoing" and data["row"]["issue"] == "0007"


def test_rebuild_replays_the_dedup_log_then_newer_assess_snapshots(runtime, layout, conn, kit):
    """problems 的重建：先重放去重的变更日志，再叠加比它新的评估快照；两边合起来是最后的状态。"""
    log = layout.run_dir(COLLECTED) / LOG.render()
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(json.dumps({"kind": PROBLEM_RECORD, "row": encode(problems.TABLE, item)})
                             for item in (row(kit, "P-0001", "new"), row(kit, "P-0002", "regressed"),
                                          row(kit, "P-0003", "new"))) + "\n", encoding="utf-8")
    persist.snapshot(layout, kit.RUN, row(kit, "P-0001", "ongoing", verdict="confirmed"))  # 评估在采集之后
    persist.snapshot(layout, EARLIER, row(kit, "P-0002", "closed"))  # 评估之后采集又判了回归
    counts = rebuild.rebuild(layout, conn)
    assert counts["problems"] == 3
    assert problems.get(conn, "P-0001").status == "ongoing"
    assert problems.get(conn, "P-0001").extra["verdict"] == "confirmed"
    assert problems.get(conn, "P-0002").status == "regressed"
    assert problems.get(conn, "P-0003").status == "new"


def test_a_broken_snapshot_stops_the_rebuild(layout, conn, kit):
    persist.snapshot(layout, kit.RUN, row(kit, "P-0001", "ongoing"))
    persist.snapshot_path(layout, "P-0001").write_text("{", encoding="utf-8")
    with pytest.raises(rebuild.RebuildError):
        persist.replay(layout, conn)


def test_a_failed_write_rolls_back_the_database_and_the_files(runtime, make_problem, layout):
    problem = make_problem()
    with pytest.raises(RuntimeError), persist.restoring(runtime, [problem.id]), transaction(runtime.conn):
        persist.update(runtime, problem, status="closed", assess={"verdict": "refuted"})
        raise RuntimeError("写到一半")
    assert problems.get(runtime.conn, "P-0001").status == "new"
    assert not persist.snapshot_path(layout, "P-0001").exists()  # 原来没有的删除，重建时不会重放没提交的改动


def test_false_positive_writes_a_suppression_for_the_fingerprint_and_aliases(runtime, make_problem):
    problem = make_problem(extra={"aliases": ["fp-old"]})
    with transaction(runtime.conn):
        persist.suppress(runtime, problem, "判为不成立")
    rules = suppress.load(runtime.conn, [])
    assert [rule.fingerprint for rule in rules] == ["fp-P-0001", "fp-old"]
    assert (rules[0].expires_on - rules[0].added_on).days == 30


def test_merge_closes_the_problem_and_moves_its_fingerprint_to_the_target(runtime, make_problem):
    target = make_problem("P-0002")
    problem = make_problem("P-0001", extra={"aliases": ["fp-older"]})
    with transaction(runtime.conn):
        persist.merge(runtime, problem, target.id, "同一根因")
    merged = problems.get(runtime.conn, "P-0001")
    assert merged.status == "closed" and merged.extra["mergedInto"] == "P-0002"
    assert "verdict" not in merged.extra  # 并入其他问题时不写评估结论
    assert problems.get(runtime.conn, "P-0002").extra["aliases"] == ["fp-P-0001", "fp-older"]
    with pytest.raises(LookupError):
        persist.merge(runtime, merged, "P-0009", "x")


def test_outcomes_are_filled_once_and_misjudgements_get_their_own_handoff(runtime, make_problem, layout):
    make_problem("P-0001", extra={"assess": {"attempt": 2, "verdict": "confirmed"}})
    make_problem("P-0002")  # 没评估过的跳过
    with transaction(runtime.conn):
        persist.fill_outcome(runtime, ["P-0001", "P-0002", "P-0009"], persist.FALSE_CONFIRM, "Issue 0007 不是缺陷")
        persist.fill_outcome(runtime, ["P-0001"], persist.CORRECT, "验收通过")  # 已有结果的不覆盖
    assert problems.get(runtime.conn, "P-0001").extra["assess"]["outcome"] == persist.FALSE_CONFIRM
    handoff = read_json(layout.problem_dir("P-0001") / "21-assess.triage.r2-handoff.json")
    assert handoff["facts"]["misjudged"] == {"kind": "false_confirm", "point": "assess.triage",
                                             "detail": "Issue 0007 不是缺陷"}
    assert handoff["facts"]["knowledgeSuggestions"] == []
    assert list(layout.problem_dir("P-0002").glob("*-handoff.json")) == []
    assert problems.get(runtime.conn, "P-0002").extra == {}


def test_retriage_requests_accumulate_notes(runtime, make_problem):
    make_problem(extra={"assess": {"manual": True, "retriageNotes": ["第一次"]}})
    with transaction(runtime.conn):
        persist.request_retriage(runtime, ["P-0001"], "修复前复现不了")
    record = problems.get(runtime.conn, "P-0001").extra["assess"]
    assert record["retriage"] and not record["manual"] and record["retriageNotes"] == ["第一次", "修复前复现不了"]
