from datetime import UTC, datetime, timedelta

import pytest

from tightrein.assess import claims
from tightrein.store.tables.occurrences import Occurrence
from tightrein.store.tables.problems import Problem

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)


def problem(source: str = "collect.platform_errors", check_type: str = "error", location: str | None = "OrderService.get",
            count: int = 1, title: str = "保存订单时报错") -> Problem:
    return Problem(id="P-0001", fingerprint="fp", source=source, check_type=check_type, status="new", title=title,
                   first_seen=NOW - timedelta(days=1), last_seen=NOW, location=location, count=count)


def occurrence(number: int = 1, *, location: str | None = None, message: str = "NullReferenceException",
               evidence: dict | None = None, minutes: int = 0, **flags: object) -> Occurrence:
    return Occurrence(problem="P-0001", seen_at=NOW + timedelta(minutes=minutes), source="collect.platform_errors",
                      commit="abc1234", id=number,
                      evidence={"signal": f"S-{number}", "checkType": "error", "location": location,
                                "message": message, **flags, "evidence": dict(evidence or {})})


@pytest.mark.parametrize("source, check_type, location, found, expected", [
    ("collect.api_fuzz", "server_error", "GET /api/orders/{id}",
     occurrence(evidence={"role": "Admin", "status": 500}, message="服务端返回 500"),
     "Admin 调用 GET /api/orders/{id} 时服务端返回 500：服务端返回 500"),
    ("collect.api_fuzz", "server_error", "GET /api/orders/{id}",
     occurrence(evidence={"role": "Viewer", "status": 200}, severityHint="P0"),
     "Viewer 不具备访问权限，调用 GET /api/orders/{id} 却返回了 200"),
    ("collect.access_log", "latency", "GET /api/orders", occurrence(message="p95 4200ms"),
     "GET /api/orders 的响应耗时超出预期：p95 4200ms"),
    ("collect.platform_errors", "error", "OrderService.get", occurrence(message="NullReferenceException: x"),
     "运行时抛出异常：NullReferenceException: x"),
    ("collect.alerts", "alert", "OrdersStalled", occurrence(message="订单 1 小时没有进展"),
     "业务告警 OrdersStalled 已触发：订单 1 小时没有进展"),
    ("collect.project_probes", "probe", "job:import", occurrence(message="导入没有按时完成"),
     "项目探针在 job:import 发现：导入没有按时完成"),
    ("collect.static", "static", "services/orders.py:3",
     occurrence(message="查询没有按公司过滤", evidence={"verification": {"verdict": "confirmed"}, "verdict": "x"}),
     "services/orders.py:3 存在以下问题：查询没有按公司过滤"),
    ("collect.incidental", "incidental:error-handling", "services/orders.py:3", occurrence(message="异常被吞掉"),
     "services/orders.py:3 存在以下问题：异常被吞掉"),
])
def test_each_probe_has_a_claim_template_without_judgements(source, check_type, location, found, expected):
    claim = claims.build(problem(source, check_type, location), [found])
    assert claim.statement == expected
    assert claim.title == "保存订单时报错"
    text = str(claim.to_json())
    assert "verification" not in text and "'verdict'" not in text  # 采集时已有的判定不交给模型


def test_facts_are_numbered_samples_and_user_notes():
    found = [occurrence(1, location="a.py:1"), occurrence(2, location="a.py:1", minutes=1),
             occurrence(3, location="a.py:1", evidence={"role": "Viewer"}, minutes=2)]
    claim = claims.build(problem(count=3), found, user_notes=["只在月底出现"], sample_limit=5)
    labels = [fact.label for fact in claim.facts]
    assert labels[:3] == ["出现次数", "首次出现", "末次出现"]
    # 按(角色, 位置)去重、从最近的取：同一处重复的只留最新一条，不挤掉其他角色
    assert labels[3:] == ["信号 S-3", "信号 S-2"]
    rendered = claim.render()
    assert "1. 出现次数：3" in rendered and "- 只在月底出现(用户提供)" in rendered
    assert claim.to_json()["facts"][0] == {"label": "出现次数", "value": 3}
    assert claims.Claim.from_json(claim.to_json()) == claim
    assert claim.with_fact("主干差异", "未修改").facts[-1] == claims.Fact("主干差异", "未修改")


def test_samples_have_a_limit():
    found = [occurrence(number, location=f"a.py:{number}", minutes=number) for number in range(1, 8)]
    assert [item.id for item in claims.samples(found, 3)] == [7, 6, 5]


def test_entry_points_come_from_routes_frames_and_static_locations():
    frames = {"projectFrames": [{"file": "services/orders.py", "line": 3, "symbol": "OrderService.get"},
                                {"file": "routes/orders.py", "line": 5}]}
    server = claims.build(problem(), [occurrence(evidence=frames), occurrence(2, evidence=frames)])
    assert server.entry_points == ("services/orders.py:3",)  # 只取首个本项目帧，去重保序
    static = claims.build(problem("collect.static", "static", "services/orders.py:3"),
                          [occurrence(location="services/orders.py:3")])
    assert static.entry_points == ("services/orders.py:3",)
    route = claims.build(problem("collect.api_fuzz", "server_error", "GET /api/orders/{id}"), [occurrence()])
    assert route.entry_points == ("GET /api/orders/{id}",)
    assert "(没有可直接得到的入口)" in claims.build(problem(), [occurrence()]).render()


def test_severity_hint_is_read_from_the_signal_record():
    assert claims.severity_hint(occurrence(severityHint="P0")) == "P0"
    assert claims.severity_hint(occurrence(evidence={"severityHint": "P0"})) is None  # 来源证据里的不算
    with pytest.raises(ValueError):
        claims.build(problem(), [])
