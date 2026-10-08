"""API 模糊测试的测试共用：锁定版本 Schemathesis 4.28.0 对桩服务实际运行一次得到的 NDJSON 事件流。

fixtures/events.json 为其中各事件组成的数组，只去掉了通过的用例、用例的 meta 与探测阶段的事件；录制时还开着越权与
响应结构等检查，解析器照常读出，映射只留服务端报错。录制时用的 token 为 RECORDED_TOKEN(出现在用例记录的请求头里)。
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "events.json"
RECORDED_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiQ29tcGFueSJ9.c3R1Yi1zaWduYXR1cmU"


@pytest.fixture
def recorded_token() -> str:
    return RECORDED_TOKEN


@pytest.fixture
def recorded_events() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def write_events(recorded_events: list[dict[str, Any]]) -> Callable[..., Path]:
    """write_events(path, items=None, drop=None)：写成 NDJSON；drop 为要去掉的事件名。"""

    def write(path: Path, items: list[dict[str, Any]] | None = None, drop: str | None = None) -> Path:
        chosen = recorded_events if items is None else items
        if drop is not None:
            chosen = [item for item in chosen if drop not in item]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in chosen) + "\n", encoding="utf-8")
        return path

    return write
