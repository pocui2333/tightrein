import sqlite3
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tightrein.domain.enums import DeploymentStatus, KnowledgeStatus, RegressionKind, RegressionResult, RunStatus
from tightrein.store.repos import deployments, knowledge, pulls, regressions, schedule_state
from tightrein.store.repos.deployments import Deployment
from tightrein.store.repos.knowledge import KnowledgeRecord
from tightrein.store.repos.pulls import PullRecord
from tightrein.store.repos.regressions import RegressionCheck
from tightrein.store.repos.schedule_state import ScheduleState

from store_samples import T0, knowledge_entry


def entry_record(entry=None, **changes):
    record = KnowledgeRecord.from_entry(entry or knowledge_entry(), "0" * 64, 1727575200.5, 2048, T0)
    return replace(record, **changes)


def document_record(**changes):
    values = dict(id="0007", type="issue", status=KnowledgeStatus.ACTIVE, title="订单查询缺少归属校验",
                  summary="订单查询缺少归属校验", updated=date(2026, 9, 30), path="issues/0007-order-owner-check.md",
                  content_sha256="1" * 64, file_mtime=1727575200.0, file_size=512, indexed_at=T0, tags=("0007",))
    values.update(changes)
    return KnowledgeRecord(**values)


def test_knowledge_entry_round_trip(conn):
    knowledge.save(conn, entry_record())
    stored = knowledge.get(conn, "DP-0012")
    assert stored == entry_record()
    assert stored.to_entry() == knowledge_entry()
    assert knowledge.by_path(conn, "knowledge/defect-pattern/DP-0012-owner-check.md") == stored
    assert knowledge.get(conn, "DP-9999") is None


def test_documents_are_active_and_not_entries(conn):
    knowledge.save(conn, document_record())
    stored = knowledge.get(conn, "0007")
    assert not stored.is_entry
    with pytest.raises(ValueError):
        stored.to_entry()
    with pytest.raises(ValueError):
        document_record(status=KnowledgeStatus.ARCHIVED)
    with pytest.raises(ValueError):
        document_record(type="weekly")


def test_saving_again_keeps_hits(conn):
    knowledge.save(conn, entry_record())
    knowledge.record_hit(conn, "DP-0012", T0 + timedelta(hours=1))
    knowledge.record_hit(conn, "DP-0012", T0 + timedelta(hours=2))
    knowledge.save(conn, entry_record(summary="新的摘要"))
    stored = knowledge.get(conn, "DP-0012")
    assert (stored.summary, stored.hits, stored.last_hit_at) == ("新的摘要", 2, T0 + timedelta(hours=2))
    assert stored.to_entry().hits == 2


def test_find_by_types_and_status(conn):
    knowledge.save(conn, entry_record())
    knowledge.save(conn, entry_record(knowledge_entry("DP-0013", path="knowledge/defect-pattern/DP-0013-x.md",
                                                      status=KnowledgeStatus.ARCHIVED)))
    knowledge.save(conn, document_record())
    assert [item.id for item in knowledge.find(conn)] == ["0007", "DP-0012", "DP-0013"]
    assert [item.id for item in knowledge.find(conn, types=["defect-pattern"], status=KnowledgeStatus.ACTIVE)] == [
        "DP-0012"]
    assert [item.id for item in knowledge.find(conn, types=["issue", "finding"])] == ["0007"]


def test_path_is_unique(conn):
    knowledge.save(conn, entry_record())
    with pytest.raises(sqlite3.IntegrityError):
        knowledge.save(conn, entry_record(knowledge_entry("DP-0013")))


def test_full_text_is_replaced_and_removed(conn):
    knowledge.save(conn, entry_record())
    knowledge.save_text(conn, "DP-0012", "缺 少 归 属", "summary", "api", "old body")
    knowledge.save_text(conn, "DP-0012", "缺 少 归 属", "summary", "api", "new body")

    def matches(query):
        return [row[0] for row in conn.execute("SELECT id FROM knowledge_fts WHERE knowledge_fts MATCH ?", (query,))]

    assert matches("body") == ["DP-0012"]
    assert matches("old") == []
    assert matches('"归 属"') == ["DP-0012"]
    knowledge.remove(conn, "DP-0012")
    assert matches("body") == []
    assert knowledge.get(conn, "DP-0012") is None


def test_deployments(conn):
    first = Deployment("abc1234", DeploymentStatus.SUCCEEDED, T0, "991", "https://ci/991", T0 - timedelta(minutes=3))
    second = Deployment("def5678", DeploymentStatus.RUNNING, T0 + timedelta(hours=1))
    deployments.save(conn, second)
    deployments.save(conn, first)
    assert deployments.get(conn, "abc1234") == first
    assert [item.commit for item in deployments.find(conn)] == ["abc1234", "def5678"]
    deployments.save(conn, replace(second, status=DeploymentStatus.SUCCEEDED))
    assert [item.commit for item in deployments.find(conn, status=DeploymentStatus.SUCCEEDED)] == ["abc1234", "def5678"]


def test_pulls(conn):
    pull = PullRecord("0007", 42, "https://github.com/o/r/pull/42", "fix/0007-order-owner-check", "修复订单归属",
                      "OPEN", T0, [{"id": 1, "author": "reviewer", "body": "请补测试", "at": "2026-10-01T00:00:00Z"}],
                      "MERGEABLE", last_checked_at=T0)
    pulls.save(conn, pull)
    assert pulls.get(conn, "0007") == pull
    pulls.save(conn, replace(pull, state="MERGED", merge_commit="fed4321", merged_at=T0 + timedelta(days=1)))
    assert pulls.find(conn, state="OPEN") == []
    assert pulls.find(conn, state="MERGED")[0].merge_commit == "fed4321"


def test_regressions_and_record_result(conn):
    check = RegressionCheck("0007", "api-1", RegressionKind.API, "regressions/0007/api-1.yaml", "2" * 64,
                            requires=("backend",), base_commit="abc1234", base_result=RegressionResult.FAILED)
    regressions.save(conn, check)
    regressions.save(conn, RegressionCheck("0007", "static-1", RegressionKind.STATIC, "regressions/0007/s.yaml",
                                           "3" * 64))
    assert regressions.get(conn, "0007", "api-1") == check
    updated = regressions.record_result(conn, "0007", "api-1", RegressionResult.PASSED,
                                        "R-20260930-000000-verify", T0, "def5678")
    assert regressions.get(conn, "0007", "api-1") == updated
    assert [item.check_id for item in regressions.find(conn, issue_id="0007")] == ["api-1", "static-1"]
    assert [item.check_id for item in regressions.find(conn, last_result=RegressionResult.NOT_RUN)] == ["static-1"]
    with pytest.raises(LookupError):
        regressions.record_result(conn, "0007", "missing", RegressionResult.PASSED, "R-x", T0, None)


def test_schedule_state(conn):
    schedule_state.save(conn, ScheduleState("static-full"))
    schedule_state.save(conn, ScheduleState("api-fuzz-deep", 2, T0, T0 + timedelta(minutes=20), RunStatus.OK,
                                            "R-20260929-021503-collect-api-fuzz"))
    assert schedule_state.get(conn, "static-full") == ScheduleState("static-full")
    assert [item.task for item in schedule_state.all_states(conn)] == ["api-fuzz-deep", "static-full"]
    assert schedule_state.get(conn, "api-fuzz-deep").last_status is RunStatus.OK
