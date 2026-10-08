from __future__ import annotations

from tightrein.implement.check.runtime.verdict import Item, Result, failed, unverified


def test_three_conclusions_and_only_failures_block() -> None:
    items = [Item("api:shallow", "api", Result.PASSED, "schemathesis run", ("events.ndjson",)),
             Item("page:/orders", "pages", Result.WEAK, reason="测试库没有订单数据"),
             unverified("screenshot:a.png", "screenshots", "页面服务没有启动"),
             Item("page:/users", "pages", Result.FAILED, reason="控制台报错")]
    assert [item.id for item in failed(items)] == ["page:/users"]
    assert items[2].result is Result.UNVERIFIED and items[2].reason == "页面服务没有启动"
    assert items[0].to_json() == {"id": "api:shallow", "category": "api", "result": "passed",
                                  "command": "schemathesis run", "evidence": ["events.ndjson"], "reason": None}
