from dataclasses import replace
from datetime import timedelta

import pytest
from pipeline_world import NOW, RELEASE, make_signal
from store_problem import make_problem
from triage_world import make_triage_world, store_problem

from tightrein.domain.enums import Probe, ProblemEvent, ProblemStatus
from tightrein.domain.problem import ProblemScope
from tightrein.pipeline.triage.steps import claims, main_diff, select
from tightrein.store.repos import problem_events, triage
from tightrein.store.repos.problem_events import OPERATION_AUTO, ProblemEventRecord
from tightrein.vcs.errors import GitCommandError
from tightrein.vcs.parse import Commit

PLATFORM_LOG = make_signal(2, probe=Probe.PLATFORM_ERRORS, check="exception", location="OrderService.Get",
                         message="NullReferenceException: Object reference not set",
                         context={"exceptionType": "NullReferenceException",
                                  "projectFrames": [{"symbol": "OrderService.Get", "line": 12,
                                                     "file": "src/Services/OrderService.src"}]}, actor={})
STATIC = make_signal(3, probe=Probe.STATIC, check="DP-0001", location="src/Services/OrderService.src:OrderService.Get",
                     message="查询没有按公司过滤", actor={},
                     context={"line": 12, "verdict": {"value": "confirmed"}, "verification": {"verdict": "confirmed"},
                              "evidence": [{"location": "src/Services/OrderService.src:12", "observation": "x"}]})


@pytest.mark.parametrize("probe, signal, scope, expected", [
    (Probe.API_FUZZ, make_signal(), "GET /api/Order/{id}",
     "Admin 调用 GET /api/Order/{id}，传入 GET /api/Order/42 时服务端返回 500"),
    (Probe.API_FUZZ, make_signal(check="unauthorized_role_access", context={
        "response": {"status": 200}, "requiredCapabilities": ["order.read"]}), "GET /api/Order/{id}",
     "Admin 不具备 order.read，调用 GET /api/Order/{id} 却返回了 200"),
    (Probe.API_FUZZ, make_signal(check="max_response_time", context={
        "request": {"path": "/api/Order/42"}, "response": {"status": 200, "elapsedMs": 4200}}), "GET /api/Order/{id}",
     "GET /api/Order/{id} 在 /api/Order/42 下耗时 4200 毫秒"),
    (Probe.API_FUZZ, make_signal(check="response_schema_conformance", message="缺少字段 total"),
     "GET /api/Order/{id}", "GET /api/Order/{id} 的响应与接口描述不一致：缺少字段 total"),
    (Probe.PLATFORM_ERRORS, PLATFORM_LOG, "OrderService.Get",
     "服务端抛出 NullReferenceException：NullReferenceException: Object reference not set"),
    (Probe.PLATFORM_ERRORS, make_signal(probe=Probe.PLATFORM_ERRORS, check="frontend-error", message="TypeError: x",
                                        context={"exceptionType": "TypeError"}), "src/app.js:render",
     "浏览器端抛出 TypeError：TypeError: x"),
    (Probe.ALERTS, make_signal(probe=Probe.ALERTS, check="business-alert", location="OrdersStalled",
                               message="订单 1 小时没有进展"), "OrdersStalled",
     "业务告警 OrdersStalled 已触发：订单 1 小时没有进展"),
    (Probe.PROJECT_PROBE, make_signal(probe=Probe.PROJECT_PROBE, check="daily-import", location="job:import",
                                      message="导入没有按时完成"), "job:import",
     "项目探针 daily-import 在 job:import 发现：导入没有按时完成"),
    (Probe.STATIC, STATIC, "src/Services/OrderService.src:OrderService.Get",
     "src/Services/OrderService.src:OrderService.Get 存在 DP-0001：查询没有按公司过滤"),
    (Probe.INCIDENTAL, make_signal(probe=Probe.INCIDENTAL, check="incidental", message="异常被吞掉",
                                   location="src/Services/OrderService.src:OrderService.Save", context={"line": 30}),
     "src/Services/OrderService.src:OrderService.Save",
     "src/Services/OrderService.src:OrderService.Save 存在以下问题：异常被吞掉"),
])
def test_each_probe_has_a_claim_template_without_judgements(probe, signal, scope, expected):
    problem = make_problem(probe=probe, scope=ProblemScope(scope, frozenset({"Admin"})))
    claim = claims.build(problem, [signal])
    assert claim.statement == expected
    assert claim.title == problem.title
    text = str(claim.to_dict())
    assert "verification" not in text and "'verdict'" not in text


def test_facts_are_numbered_samples_and_user_notes():
    problem = make_problem(occurrences=3)
    found = [make_signal(1), make_signal(2, occurred_at=NOW + timedelta(minutes=1)),
             make_signal(3, actor={"role": "Viewer"})]
    claim = claims.build(problem, found, user_notes=["只在月底出现"], sample_limit=5)
    labels = [fact.label for fact in claim.facts]
    assert labels[:3] == ["出现次数", "首次出现", "末次出现"]
    assert labels[3:5] == [f"信号 {found[1].id}", f"信号 {found[2].id}"]
    assert claim.entry_points == ("GET /api/Order/{id}",)
    rendered = claim.render()
    assert "1. 出现次数：3" in rendered and "- 只在月底出现(用户提供)" in rendered
    assert claim.to_dict()["facts"][0] == {"label": "出现次数", "value": 3}


def test_entry_points_come_from_frames_and_static_locations():
    server_problem = make_problem(probe=Probe.PLATFORM_ERRORS, scope=ProblemScope("OrderService.Get"))
    server = claims.build(server_problem, [PLATFORM_LOG])
    assert server.entry_points == ("src/Services/OrderService.src:12",)
    static = claims.build(make_problem(probe=Probe.STATIC, scope=ProblemScope(STATIC.location)), [STATIC])
    assert static.entry_points == ("src/Services/OrderService.src:12",)


def test_ordering_puts_p0_first_then_server_errors_regressions_runtime_and_static(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0001", make_signal(1, probe=Probe.STATIC, check="DP-0001", actor={}))
    store_problem(world, "P-0002", make_signal(2, check="response_schema_conformance"))
    store_problem(world, "P-0003", make_signal(3, check="response_schema_conformance"), status=ProblemStatus.REGRESSED)
    store_problem(world, "P-0004", make_signal(4))
    store_problem(world, "P-0005", make_signal(5, check="unauthorized_role_access"))
    store_problem(world, "P-0006", make_signal(6), last_seen_at=NOW + timedelta(hours=1))
    assert [problem.id for problem in select.pending(world.conn)] == [
        "P-0005", "P-0006", "P-0004", "P-0003", "P-0002", "P-0001"]
    assert [problem.id for problem in select.choose(world.conn, 2).chosen] == ["P-0005", "P-0006"]


def _event(world, problem_id, event, to_status, at, from_status=ProblemStatus.PENDING):
    problem_events.append(world.conn, ProblemEventRecord(problem_id, at, event, OPERATION_AUTO, from_status,
                                                         to_status, run_id="R-20261005-030000-aggregate"))


def test_a_problem_is_triaged_once_until_it_regresses_or_a_retriage_is_requested(tmp_path):
    from tightrein.domain.enums import Disposition, Verdict
    from tightrein.domain.triage import TriageResult
    from tightrein.store.repos.triage import TriageRecord

    world = make_triage_world(tmp_path)
    problem = store_problem(world)
    _event(world, "P-0001", ProblemEvent.REPRODUCED, ProblemStatus.NEW, NOW - timedelta(hours=2))
    assert select.eligible(world.conn, problem)
    triage.save(world.conn, TriageRecord(TriageResult("P-0001", 1, Verdict.INSUFFICIENT, Disposition.MANUAL_QUEUE,
                                                      "缺少信息", "c" * 40), "R-20261005-020000-triage",
                                         NOW - timedelta(hours=1)))
    assert not select.eligible(world.conn, problem)
    assert select.choose(world.conn, 5, ("P-0001",)).rejected == [
        ("P-0001", "P-0001 已分诊，重新分诊请用 tightrein retriage")]
    assert select.choose(world.conn, 5, ("P-0001",), retriage=True).chosen == [problem]
    _event(world, "P-0001", ProblemEvent.RETRIAGE_REQUESTED, None, NOW, ProblemStatus.NEW)
    assert select.eligible(world.conn, problem)
    regressed = replace(problem, status=ProblemStatus.REGRESSED)
    _event(world, "P-0001", ProblemEvent.SEEN_AGAIN, ProblemStatus.REGRESSED, NOW, ProblemStatus.RESOLVED)
    assert select.eligible(world.conn, regressed)


def test_merged_ignored_and_missing_problems_are_rejected_with_reasons(tmp_path):
    world = make_triage_world(tmp_path)
    store_problem(world, "P-0003", make_signal(3))
    store_problem(world, "P-0001", merged_into="P-0003")
    store_problem(world, "P-0002", make_signal(2), status=ProblemStatus.IGNORED)
    rejected = select.choose(world.conn, 5, ("P-0001", "P-0002", "P-0009"), retriage=True).rejected
    assert rejected[0] == ("P-0001", "P-0001 已并入 P-0003")
    assert rejected[1][1].startswith("P-0002 状态为 已忽略")
    assert rejected[2] == ("P-0009", "P-0009 不存在")
    assert select.choose(world.conn, 5, ("P-0002",), ignore_state=True).chosen[0].id == "P-0002"


class FakeLog:
    def __init__(self, commits=(), error=None):
        self.commits, self.error, self.calls = list(commits), error, []

    def log(self, repo, rev_range, paths=None, limit=None):
        self.calls.append((rev_range, tuple(paths)))
        if self.error:
            raise self.error
        return self.commits


def test_main_diff_lists_commits_touching_candidate_files(tmp_path):
    claim = claims.build(make_problem(probe=Probe.PLATFORM_ERRORS, scope=ProblemScope("OrderService.Get")),
                         [PLATFORM_LOG])
    files = main_diff.candidate_files(claim, [PLATFORM_LOG])
    assert files == ("src/Services/OrderService.src",)
    commit = Commit("d" * 40, "zhang", NOW, "修复订单查询")
    log = FakeLog([commit])
    diff = main_diff.collect(log, tmp_path, RELEASE, "c" * 40, files)
    assert log.calls == [(f"{RELEASE}..{'c' * 40}", files)]
    assert diff.value()["commits"] == [{"commit": "d" * 40, "subject": "修复订单查询"}]
    unchanged = main_diff.collect(FakeLog(), tmp_path, RELEASE, "c" * 40, files)
    assert "期间候选文件期间未修改" in unchanged.value()
    assert main_diff.collect(FakeLog(), tmp_path, None, "c" * 40, files).value() == "发现问题时的版本未知，未比较"
    failed = main_diff.collect(FakeLog(error=GitCommandError("bad revision")), tmp_path, RELEASE, "c" * 40, files)
    assert failed.value().startswith("查询提交记录失败")
    route_only = claims.build(make_problem(), [make_signal()])
    assert main_diff.candidate_files(route_only, [make_signal()]) == ()
