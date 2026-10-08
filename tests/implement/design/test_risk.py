"""风险判定：按路径、改动内容、评估与方案的标记、影响类别命中；规则由配置给出，不写死。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from tightrein.agents.params import HIGH_RISK
from tightrein.implement.context import Risk
from tightrein.implement.design import risk
from tightrein.implement.design.risk import Changed

RULES = {"controls": {"implement.design": {"riskRules": {
    "schema": {"paths": ["db/schema/"], "patterns": [r"ALTER\s+TABLE"]},
    "authz": {"paths": ["src/policies/"], "patterns": [r"@login_required"]},
    "contract": {"paths": ["api/openapi.yaml"], "patterns": []},
}}}}


def test_paths_and_changed_lines(new_world: Any, settings_with: Any) -> None:
    settings = new_world(settings=settings_with(RULES)).runtime.settings
    found = risk.judge([Changed("db/schema/orders.rb"), Changed("src/views.py", added=("    @login_required",)),
                        Changed("api/openapi.yaml")], flags={}, impact=None, migration=False, settings=settings)
    assert found.high and found.reasons == (
        "schema：db/schema/orders.rb 命中路径 db/schema/",
        "authz：src/views.py 的改动行命中内容规则：@login_required",
        "contract：api/openapi.yaml 命中路径 api/openapi.yaml",
    )


def test_markers_migrations_impact_and_protected_files(world: Any) -> None:
    settings = world.runtime.settings
    found = risk.judge([Changed("pyproject.toml")], flags={"dataStructure": True, "publicContract": False},
                       impact="authorization", migration=True, settings=settings)
    assert found.reasons == ("schema：标记 dataStructure", "schema：含数据库迁移", "authz：影响类别 authorization",
                             "protected：pyproject.toml 是高风险文件")
    assert found.high_risk_paths == ("pyproject.toml",)


def test_misses_are_normal(world: Any) -> None:
    found = risk.judge([Changed("src/orders.py", added=("return days * 7",))], flags={}, impact="display",
                       migration=False, settings=world.runtime.settings)
    assert found == Risk(False, (), ())
    assert risk.conditions(found) == () and risk.conditions(None) == ()
    assert risk.conditions(Risk(True, ("x",))) == (HIGH_RISK,)


def test_the_judgements_before_and_after_the_plan_and_after_the_changes(new_world: Any, settings_with: Any) -> None:
    world = new_world(settings=settings_with(RULES))
    context = world.context()
    context.issue = replace(context.issue, extra={"flags": {"publicContract": True}})
    before = risk.plan_risk(context, world.runtime.settings)
    assert before.reasons == ("contract：标记 publicContract",)
    plan = {"files": [{"path": "src/orders.py"}], "flags": {"dataStructure": {"flagged": True}}, "migration": None}
    assert risk.design_risk(plan, context, world.runtime.settings).reasons == (
        "schema：标记 dataStructure", "contract：标记 publicContract")
    # 自检后按实际改动的行再判一次
    applied = risk.apply_risk([Changed("src/orders.py", removed=("ALTER TABLE orders",))], {"flags": {}}, context,
                              world.runtime.settings)
    assert applied.reasons[0] == "schema：src/orders.py 的改动行命中内容规则：ALTER TABLE orders"
    assert risk.to_facts(applied)["high"] is True


def test_files_holding_endpoint_handlers_are_contract_changes(new_world: Any, settings_with: Any) -> None:
    inventory = {"controls": {"implement.check.runtime": {"api": {"endpoints": "endpoints.json"}}}}
    world = new_world(settings=settings_with(inventory))
    (world.repo.path / "endpoints.json").write_text(
        json.dumps([{"method": "GET", "route": "/api/orders", "sourceFile": "src/api/orders.py"},
                    {"method": "GET", "route": "/health"}]), encoding="utf-8")
    context = world.context()
    assert risk.endpoint_files(context, world.runtime.settings) == {"src/api/orders.py"}
    applied = risk.apply_risk([Changed("src/api/orders.py", added=("    return rows",)), Changed("src/util.py")],
                              {"flags": {}}, context, world.runtime.settings)
    assert applied.high and applied.reasons == ("contract：src/api/orders.py 是端点处理方法所在的文件",)
    # 没有配置端点清单时不按它判
    other = new_world()
    assert risk.endpoint_files(other.context(), other.runtime.settings) == frozenset()
