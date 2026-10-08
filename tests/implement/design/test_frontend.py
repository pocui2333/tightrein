"""前端文件的判定：项目事实中写明的模式加上缺省模式；没有前端文件时不调用模型。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from tightrein.implement.design import frontend


def test_frontend_files_follow_the_project_and_default_patterns(world: Any) -> None:
    paths = ["src/orders.py", "web/Orders.vue", "client/app.ts", "styles/main.css"]
    assert frontend.frontend_files(world.runtime, paths) == ["web/Orders.vue", "styles/main.css"]
    settings = world.runtime.settings
    settings.project = replace(settings.project, frontend_patterns=("client/",))
    assert frontend.frontend_files(world.runtime, paths) == ["web/Orders.vue", "client/app.ts", "styles/main.css"]


def test_backend_only_plans_skip_the_designer(world: Any, monkeypatch: Any) -> None:
    def fail(*args: Any) -> None:
        raise AssertionError("不该调用模型")

    monkeypatch.setattr(frontend, "ask", fail)
    plan = {"files": [{"path": "src/orders.py", "isNew": False, "reason": None}]}
    assert frontend.design(world.runtime, world.context(), plan, None) is None  # type: ignore[arg-type]
