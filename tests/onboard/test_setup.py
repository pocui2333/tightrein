import json
from pathlib import Path
from typing import Any

import pytest

from tightrein.onboard.setup import MODULES, ModuleStatus, SetupInvalid, load
from tightrein.store.files.layout import WorkspaceLayout


def module(status: str = "enabled", **values: Any) -> dict[str, Any]:
    return {"status": status, "method": None, "script": None, "guide": None, "reason": None, "impact": None,
            **values}


def write(layout: WorkspaceLayout, modules: dict[str, Any]) -> None:
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.setup.write_text(json.dumps({"project": "shop", "updatedAt": "2026-10-08T00:00:00Z",
                                        "modules": modules}), encoding="utf-8")


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    return WorkspaceLayout(tmp_path / "workspaces" / "shop")


def every(**changes: dict[str, Any]) -> dict[str, Any]:
    return {key: changes.get(key, module()) for key in MODULES}


def test_a_complete_setup_is_read(layout):
    (layout.root / "scripts").mkdir(parents=True)
    (layout.root / "scripts" / "access.py").write_text("", encoding="utf-8")
    write(layout, every(**{
        "collect.alerts": module("disabled", reason="没有 Alertmanager"),
        "collect.access_log": module("custom", script="scripts/access.py", guide="02-db.md", reason="存在库里",
                                     secrets=["db.password"]),
    }))
    setup = load(layout)
    assert setup.project == "shop" and set(setup.modules) == set(MODULES)
    assert setup.module("collect.access_log").status is ModuleStatus.CUSTOM
    assert setup.module("collect.access_log").secrets == ("db.password",)
    assert setup.enabled("collect.access_log") and not setup.enabled("collect.alerts")
    assert [item.key for item in setup.blind_spots()] == ["collect.alerts"]  # 不启用且没写兜底


def test_a_missing_module_or_file_is_an_error_not_a_default(layout):
    with pytest.raises(SetupInvalid, match="文件不存在"):
        load(layout)
    modules = every()
    del modules["release.deploy"]
    modules["collect.unknown"] = module()
    write(layout, modules)
    with pytest.raises(SetupInvalid) as raised:
        load(layout)
    assert "modules.release.deploy：漏写；每个模块都要明确列出" in raised.value.issues
    assert "modules.collect.unknown：不认识的模块" in raised.value.issues


def test_required_fields_follow_the_status_and_every_problem_is_listed(layout):
    incomplete = module("disabled")
    del incomplete["impact"]
    write(layout, every(**{
        "collect.alerts": incomplete,
        "collect.static": module("custom", script="scripts/missing.py"),
        "collect.api_fuzz": module("sometimes"),
        "collect.incidental": module(secrets=["sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"]),
    }))
    with pytest.raises(SetupInvalid) as raised:
        load(layout)
    issues = raised.value.issues
    assert "modules.collect.alerts.impact：缺少(不适用写 null)" in issues
    assert "modules.collect.alerts.reason：disabled 时必填" in issues
    assert "modules.collect.static.guide：custom 时必填" in issues
    assert "modules.collect.static.script：脚本不存在：scripts/missing.py" in issues
    assert "modules.collect.api_fuzz.status：只能是 enabled、disabled、custom" in issues
    assert "modules.collect.incidental.secrets：只写凭据条目名，不写值" in issues
    with pytest.raises(KeyError):
        load_valid(layout).module("collect.nope")


def load_valid(layout: WorkspaceLayout) -> Any:
    write(layout, every())
    return load(layout)


def test_switches_cannot_be_custom_and_are_not_blind_spots(layout):
    (layout.root / "scripts").mkdir(parents=True)
    (layout.root / "scripts" / "accept.py").write_text("", encoding="utf-8")
    write(layout, every(**{"release.accept": module("custom", script="scripts/accept.py", guide="g.md", reason="自己做")}))
    with pytest.raises(SetupInvalid) as raised:
        load(layout)
    assert "modules.release.accept.status：只能是 enabled 或 disabled(这一项只是开关)" in raised.value.issues
    write(layout, every(**{"release.accept": module("disabled", reason="不观察"),
                           "release.github_issues": module("disabled", reason="只记本地")}))
    setup = load(layout)
    assert not setup.enabled("release.accept") and setup.blind_spots() == []  # 不启用的影响是确定的
