"""定位：笔记够用就跳过；位置先补全再核对，不合格带原因重做；通过后程序截取原文补进代码笔记。"""

from __future__ import annotations

from typing import Any

import pytest

from tightrein.agents.result import CallResult, CallStatus
from tightrein.assess import notes as notes_file
from tightrein.assess.notes import CORE, RELATED, CodeNotes, NoteEntry
from tightrein.collect.incidental.handoffs import FINDING_SCHEMA
from tightrein.implement.locate import locate
from tightrein.protocol.handoff import Status, load_schema, schema_errors

SERVICE = "class Service:\n    def run(self):\n        return helper(\n            1)\n"
INCIDENTAL = {"file": "src/users.py", "line": 1, "symbol": None, "category": "defect", "confidence": "suspected",
              "evidence": "NAME 写死为 'a'", "text": "用户名写死"}


def _output(core: list[str], related: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"knowledgeSuggestions": ["订单查询都经 recent()"], "analysis": "从路由追到 recent()",
            "core": [{"location": item, "description": "只取当天"} for item in core],
            "related": [{"location": item, "description": "调用方"} for item in related],
            "trigger": "跨天查询时", "affectedEndpoints": ["GET /api/orders"], "affectedPages": [], "missing": [],
            "incidentalFindings": [INCIDENTAL]}


class FakeAsk:
    """依次给出预置结果(None 表示调用失败)；输出先按 locate.schema.json 校验，用量照真实的 ask 记进 usage。"""

    def __init__(self, *outputs: dict[str, Any] | None) -> None:
        self.outputs = list(outputs)
        self.requests: list[Any] = []
        self.schema = load_schema(locate.SCHEMA)

    def __call__(self, runtime: Any, context: Any, request: Any, usage: Any) -> CallResult:
        self.requests.append(request)
        output = self.outputs.pop(0)
        if output is None:
            result = CallResult(CallStatus.TIMEOUT, "claude", "opus", error="超时")
        else:
            assert schema_errors(output, self.schema) == []
            result = CallResult(CallStatus.OK, "claude", "opus", output=output)
        usage.add(result, "fake")
        return result


def _install(monkeypatch: pytest.MonkeyPatch, *outputs: dict[str, Any] | None) -> FakeAsk:
    fake = FakeAsk(*outputs)
    monkeypatch.setattr(locate, "ask", fake)
    return fake


def test_sufficient_notes_skip_the_model(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    context = world.context()
    context.notes = CodeNotes("0018", world.repo.base, [NoteEntry("src/orders.py:2", CORE, "只取当天")],
                              ["src/orders.py"])
    result = locate.run(world.runtime, context)
    assert result.status is Status.PASSED and fake.requests == []
    assert result.facts["skipped"] == locate.SKIPPED and result.facts["files"] == ["src/orders.py"]


def test_locations_are_completed_checked_and_fed_back(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    world.repo.write({"src/service.py": SERVICE})
    fake = _install(monkeypatch, _output(["src/orders.py:9"]), _output(["orders.py:2"], ("src/service.py:3",)))
    result = locate.run(world.runtime, world.context())
    assert result.status is Status.PASSED
    # 第一次的位置越界：带原因重做
    assert fake.requests[1].variables["feedback"] == "- 位置不存在或越界：src/orders.py 只有 2 行，引用了第 9 行"
    saved = notes_file.load(world.runtime.workspace, "0018")
    assert saved is not None and saved.files == ["src/orders.py", "src/service.py"]
    core, related = saved.entries
    assert (core.location, core.role) == ("src/orders.py:2", CORE)  # 只写了文件名，按唯一后缀补全
    assert "def recent(days):" in (core.excerpt or "")  # 原文由程序截取
    # 定义行只认关键字开头的写法：`return helper(` 不是定义
    assert (related.role, related.signature) == (RELATED, "2: def run(self):")
    facts = result.facts
    assert facts["skipped"] is None and facts["baseCommit"] == world.repo.base
    assert facts["knowledgeSuggestions"] == ["订单查询都经 recent()"]
    assert facts["affectedEndpoints"] == ["GET /api/orders"] and facts["trigger"] == "跨天查询时"
    # 任务外发现照采集的格式写，由 collect.incidental 读取
    assert facts["incidentalFindings"] == [INCIDENTAL]
    assert all(schema_errors(item, load_schema(FINDING_SCHEMA)) == [] for item in facts["incidentalFindings"])
    assert result.notes == "从路由追到 recent()" and result.metrics.calls == 2


def test_stale_core_locations_are_asked_again(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _output(["src/orders.py:2"]))
    context = world.context()
    context.notes = CodeNotes("0018", world.repo.base, [NoteEntry("src/orders.py:30", CORE, "代码变了")])
    assert locate.run(world.runtime, context).status is Status.PASSED
    assert "已不成立，重新确认：src/orders.py:30" in fake.requests[0].variables["feedback"]


def test_it_stops_after_the_rounds(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, None, _output(["src/missing.py:1"]))  # controls 的 rounds 缺省为 1：共两次
    result = locate.run(world.runtime, world.context())
    assert result.status is Status.FAILED and result.facts["skipped"] is None
    assert len(result.facts["problems"]) == 2 and "timeout" in result.facts["problems"][0]
    assert notes_file.load(world.runtime.workspace, "0018") is None
