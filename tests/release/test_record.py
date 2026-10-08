"""发布记录：交付事实的解析(改动文件两种写法、缺字段报错)、发布进度的往返、交接落盘。"""

import pytest

from tightrein.protocol import handoff
from tightrein.protocol.handoff import Status
from tightrein.protocol.naming import FileName
from tightrein.release.record import (
    POINT_PR,
    DeliveryMissing,
    ReleaseState,
    delivery,
    load_state,
    parse_delivery,
    save_state,
    write_handoff,
)
from tightrein.store.tables import issues


def test_delivery_accepts_paths_or_change_records(kit, tmp_path):
    facts = kit.delivery_facts(tmp_path, ["src/order.py"], changedFiles=["src/b.py", {"path": "src/a.py"}],
                               acceptedFindings=["lint：行太长"], highRiskPaths=["package.json"])
    found = parse_delivery(facts, "0007")
    assert found.changed_files == ("src/a.py", "src/b.py")
    assert found.accepted == ("lint：行太长",)
    assert found.checks_passed
    assert found.review_round == 1 and found.review == ("light：通过",)
    assert found.text.summary == "订单页翻页从第一页开始" and found.text.scope is None


def test_missing_delivery_facts_are_listed(kit, tmp_path):
    facts = kit.delivery_facts(tmp_path, ["src/order.py"], branch="", diffHash=None)
    with pytest.raises(DeliveryMissing, match="branch、diffHash"):
        parse_delivery(facts, "0007")


def test_the_latest_delivery_handoff_is_read(kit, runtime, tmp_path):
    with pytest.raises(DeliveryMissing):
        delivery(runtime, "0007")
    kit.deliver(runtime, "0007", kit.delivery_facts(tmp_path, ["src/order.py"]))
    assert delivery(runtime, "0007").branch == kit.BRANCH


def test_release_state_round_trips_through_the_issue(kit, runtime):
    issue = kit.new_issue(runtime)
    state = load_state(issue)
    assert state == ReleaseState()
    state.pushed, state.merge_reasons = "abc", ["CI 检查还在进行"]
    save_state(runtime, issue, state)
    stored = issues.get(runtime.conn, "0007")
    assert load_state(stored).pushed == "abc"
    assert ReleaseState.from_json({**state.to_json(), "unknown": 1}).merge_reasons == ["CI 检查还在进行"]


def test_each_step_writes_a_handoff(runtime):
    path = write_handoff(runtime, "0007", POINT_PR, Status.PASSED, "PR #186", {"pr": 186}, started=0.0)
    assert path == runtime.workspace.step_file("0007", FileName(POINT_PR, "handoff", "json"))
    assert path.name == "41-release.pr-handoff.json"
    written = handoff.read(path)
    assert written.facts == {"pr": 186} and written.metrics.duration_ms is not None
