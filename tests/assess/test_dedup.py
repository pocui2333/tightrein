from datetime import timedelta

import pytest

from tightrein.agents.result import CallStatus
from tightrein.assess import claims, dedup
from tightrein.assess.checks import Snapshot
from tightrein.protocol.naming import format_iso
from tightrein.store.tables import occurrences

SAME = {"sameRootCause": True, "target": "0007", "evidence": ["services/orders.py:3"], "reason": "同一处查询没有过滤"}
DIFFERENT = {"sameRootCause": False, "target": None, "evidence": [], "reason": "不同接口的不同缺陷"}


@pytest.fixture
def params(runtime):
    return dedup.Params.from_settings(runtime.settings.section("assess"),
                                      int(runtime.settings.control("assess.dedup", "rounds")))


def assessed(make_problem, now, problem_id, *, days, location="services/orders.py:9", title="另一个问题",
             cause="services/orders.py", **extra):
    record = {"at": format_iso(now - timedelta(days=days)),
              "rootCauses": [{"file": cause, "line": 9, "symbol": None}], "destination": "watch"}
    return make_problem(problem_id, status="muted", location=location, title=title,
                        extra={"assess": record, **extra})


@pytest.fixture
def world(make_problem, make_issue, kit):
    make_problem("P-0002", status="ongoing", issue="0007", title="订单详情接口报错")
    make_issue("0007", status="todo", problems_=["P-0002"], title="订单详情接口报错",
               extra={"rootCauses": [{"file": "services/orders.py", "line": 3, "symbol": "OrderService.get"}]})
    make_issue("0008", status="done", problems_=["P-0002"], extra={"closeReason": "fixed"})
    assessed(make_problem, kit.NOW, "P-0003", days=10)
    assessed(make_problem, kit.NOW, "P-0004", days=200)
    assessed(make_problem, kit.NOW, "P-0005", days=1, mergedInto="P-0003")
    assessed(make_problem, kit.NOW, "P-0006", days=1, location="GET /other", title="完全无关的告警",
             cause="jobs/import.py")
    return make_problem


def target_problem(make_problem, title="保存订单时报错"):
    return make_problem("P-0001", title=title)


def test_candidates_are_open_issues_and_recent_assessed_problems(world, conn, kit):
    problem = target_problem(world)
    found = {item.candidate.id: item for item in dedup.candidates(conn, problem, [], kit.NOW, 90)}
    assert sorted(found) == ["0007", "P-0003", "P-0006"]  # 已关闭的 Issue、太久以前的、已并入的不算
    assert found["0007"].same_location and found["0007"].candidate.root_causes == ("services/orders.py:3",)
    assert found["P-0003"].file_hit and not found["P-0003"].same_location
    assert not found["P-0006"].file_hit and not found["P-0006"].same_location


def test_the_program_decides_obvious_cases_without_the_model(world, runtime, repo, params, agent):
    problem = target_problem(world, title="订单详情接口报错")  # 与 0007 的问题同位置、同标题
    claim = claims.build(problem, occurrences.find(runtime.conn, problem.id))
    outcome = dedup.decide(runtime, problem, claim, [], Snapshot(repo), params)
    assert outcome.target == "P-0002" and outcome.decided_by == dedup.PROGRAM  # Issue 并入它的第一个问题
    assert agent.calls == []
    unrelated = dedup.screen([], params)
    assert unrelated == (None, [])


def test_unsure_candidates_go_to_the_model_and_its_answer_is_checked(world, runtime, repo, params, agent):
    problem = target_problem(world)
    claim = claims.build(problem, occurrences.find(runtime.conn, problem.id))
    agent.queue("assess.dedup", dict(SAME, target="0099"), SAME)
    outcome = dedup.decide(runtime, problem, claim, [], Snapshot(repo), params)
    assert outcome.target == "P-0002" and outcome.decided_by == dedup.MODEL
    retry = agent.calls[1].prompt
    assert "target 0099 不在候选中" in retry  # 不合格的带原因交回重做
    candidates = agent.calls[0].prompt
    assert "0007" in candidates and "P-0003" in candidates and "P-0006" not in candidates  # 明显不相关的不交给模型


def test_an_invalid_or_failed_answer_means_a_different_root_cause(world, runtime, repo, params, agent):
    problem = target_problem(world)
    claim = claims.build(problem, occurrences.find(runtime.conn, problem.id))
    agent.queue("assess.dedup", dict(SAME, evidence=["services/orders.py:99"]), CallStatus.SCHEMA_INVALID)
    outcome = dedup.decide(runtime, problem, claim, [], Snapshot(repo), params)
    assert outcome.target is None and "按不同根因继续评估" in outcome.note  # 错误合并会吞掉真问题
    agent.queue("assess.dedup", DIFFERENT)
    assert dedup.decide(runtime, problem, claim, [], Snapshot(repo), params).target is None


def test_the_check_requires_a_given_target_and_real_locations(repo):
    found = [dedup.Candidate("P-0003", dedup.PROBLEM, "t", None, (), "watch")]
    assert dedup.check(DIFFERENT, found, Snapshot(repo)) == []
    reasons = dedup.check({"sameRootCause": True, "target": "P-0009", "evidence": []}, found, Snapshot(repo))
    assert len(reasons) == 2 and "P-0003" in reasons[0] and "evidence" in reasons[1]


def test_a_user_issue_without_problems_is_no_merge_target(runtime, make_issue):
    make_issue("0009", origin="user", status="todo")
    candidate = dedup.Candidate("0009", dedup.ISSUE, "t", None, (), "todo")
    assert dedup.merge_target(runtime.conn, candidate) is None
