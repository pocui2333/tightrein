from datetime import timedelta

from tightrein.assess import select
from tightrein.store.tables import problems


def test_a_problem_is_assessed_once_until_it_regresses_or_a_retriage_is_requested(conn, clock, make_problem):
    problem = make_problem()
    assert select.eligible(problem)
    assessed = problems.get(conn, "P-0001")
    assessed.status, assessed.extra = "ongoing", {"assess": {"attempt": 1, "verdict": "confirmed"}}
    problems.save(conn, assessed, clock)
    assert not select.eligible(assessed)
    assert select.choose(conn, 5, ["P-0001"]).rejected[0][1].startswith("P-0001 状态为 ongoing")
    assert select.choose(conn, 5, ["P-0001"], retriage=True).chosen[0].id == "P-0001"
    assessed.extra["assess"]["retriage"] = True  # 用户请求了重新评估
    assert select.eligible(assessed)
    assessed.extra["assess"]["retriage"] = False
    assessed.status = "regressed"  # 回归即说明有新情况
    assert select.eligible(assessed)
    manual = make_problem("P-0002", extra={"assess": {"manual": True}})
    assert not select.eligible(manual)  # 转人工的不再自动评估
    assert select.choose(conn, 5, ["P-0002"]).rejected == [
        ("P-0002", "P-0002 已评估，重新评估请用 tightrein problem retriage")]


def test_merged_ignored_and_missing_problems_are_rejected_with_reasons(make_problem, conn):
    make_problem("P-0003")
    make_problem("P-0001", extra={"mergedInto": "P-0003"})
    make_problem("P-0002", status="muted")
    rejected = select.choose(conn, 5, ["P-0001", "P-0002", "P-0009"], retriage=True).rejected
    assert rejected[0] == ("P-0001", "P-0001 已并入 P-0003，请针对 P-0003 操作")
    assert rejected[1][1].startswith("P-0002 状态为 muted") and "reopen" in rejected[1][1]
    assert rejected[2] == ("P-0009", "P-0009 不存在")


def test_ordering_puts_p0_first_then_server_errors_regressions_runtime_and_static(make_problem, conn, kit):
    later = kit.NOW + timedelta(hours=1)
    make_problem("P-0001", source="collect.static", check_type="static")
    make_problem("P-0002", source="collect.alerts", check_type="alert")
    make_problem("P-0003", source="collect.alerts", check_type="alert", status="regressed")
    make_problem("P-0004", source="collect.api_fuzz", check_type="server_error", location="GET /a")
    make_problem("P-0005", source="collect.api_fuzz", check_type="server_error", location="GET /b",
                 flags={"severityHint": "P0"})
    make_problem("P-0006", source="collect.platform_errors", check_type="error", seen=later)
    make_problem("P-0007", source="collect.incidental", check_type="incidental:error-handling")
    assert [problem.id for problem in select.pending(conn)] == [
        "P-0005", "P-0006", "P-0004", "P-0003", "P-0002", "P-0001", "P-0007"]
    assert [problem.id for problem in select.choose(conn, 2).chosen] == ["P-0005", "P-0006"]


def test_related_problems_share_a_group_and_keep_their_order(make_problem):
    first = make_problem("P-0001", location="services/orders.py:3")
    second = make_problem("P-0002", location="routes/orders.py:4")
    third = make_problem("P-0003", location="services/orders.py:9")  # 同一文件
    fourth = make_problem("P-0004", location=None)
    groups = select.related_groups([first, second, third, fourth])
    assert [[problem.id for problem in group] for group in groups] == [["P-0001", "P-0003"], ["P-0002"], ["P-0004"]]
