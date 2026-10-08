from tightrein.onboard.check import Trial
from tightrein.onboard.render import Page, refresh, render
from tightrein.onboard.setup import FIELDS, MODULES
from tightrein.store.files.json import write_json
from tightrein.store.files.layout import WorkspaceLayout


def setup(**overrides: dict) -> dict:
    modules = {key: {**dict.fromkeys(FIELDS), "status": "enabled"} for key in MODULES}
    for key, values in overrides.items():
        modules[key.replace("__", ".")].update(values)
    return {"project": "shop", "updatedAt": "2026-10-08T00:00:00Z", "modules": modules}


def page(data: dict, **extra) -> Page:
    values = {"project": "shop", "setup": data, "facts": {"repo": "/r", "mainBranch": "main", "commands": {}},
              "issues": [], "trials": [], "checked_at": None, "hints": {}}
    return Page(**{**values, **extra})


def test_the_summary_counts_states_and_lists_blind_spots_and_items_to_fill():
    data = setup(collect__alerts={"status": "disabled", "reason": "没有 Alertmanager", "impact": None},
                 collect__access_log={"status": "disabled", "reason": "没有日志", "impact": "靠平台错误"},
                 collect__project_probes={"status": "custom", "script": "scripts/p.py", "guide": "g.md",
                                          "reason": "自己写"},
                 release__deploy={"status": None})
    text = render(page(data, facts={"repo": "/r", "mainBranch": None, "commands": {"test": None, "lint": "x"}}), "zh")
    assert "启用 7 项，不启用 2 项，自定义 1 项，待填 1 项" in text
    blind = text.split("### 盲区(不启用且没有兜底)")[1].split("###")[0]
    assert "- collect.alerts" in blind and "collect.access_log" not in blind
    pending = text.split("### 待填")[1].split("##")[0]
    assert "- release.deploy" in pending and "project.mainBranch" in pending and "project.commands.test" in pending
    assert "| `collect.project_probes` | 自定义 | scripts/p.py(g.md) | 自己写 |  | 未试跑 |" in text


def test_trials_and_the_baseline_are_shown():
    trials = [Trial("collect.alerts", "failed", "连不上"), Trial("baseline.test", "passed", "`pytest` 在 abc 上通过")]
    text = render(page(setup(), trials=trials, checked_at="2026-10-08T01:00:00Z"), "zh")
    assert "上次试跑：2026-10-08T01:00:00Z" in text
    assert "| 不通过：连不上 |" in text
    assert "- `baseline.test`：通过：`pytest` 在 abc 上通过" in text


def test_problems_and_unknown_languages_fall_back_to_english():
    text = render(page({"modules": {}}, issues=["modules.collect.alerts：漏写"]), "ja")
    assert text.startswith("# Setup: shop") and "- modules.collect.alerts：漏写" in text
    assert "to fill 11" in text


def test_refresh_reads_the_current_files(tmp_path):
    layout = WorkspaceLayout(tmp_path / "shop")
    write_json(layout.setup, setup(collect__alerts={"status": "disabled"}))
    write_json(layout.settings, {"project": {"repo": "/r", "mainBranch": "main"}, "overrides": {}})
    path = refresh(layout, trials=[], checked_at=None, language="zh")
    text = path.read_text(encoding="utf-8")
    assert path == layout.setup_md and "modules.collect.alerts.reason：disabled 时必填" in text


def test_disabled_switches_show_their_known_impact_and_are_not_blind_spots():
    data = setup(release__accept={"status": "disabled", "reason": "不观察"},
                 release__github_issues={"status": "disabled", "reason": "只记本地"})
    text = render(page(data), "zh")
    assert "| `release.accept` | 不启用 |  | 不观察 | 合并并部署即完成，不观察回归 |" in text
    assert "| `release.github_issues` | 不启用 |  | 只记本地 | Issue 只记在本地，不建 GitHub 镜像 |" in text
    blind = text.split("### 盲区(不启用且没有兜底)")[1].split("###")[0]
    assert "release." not in blind
    assert "Done once merged and deployed" in render(page(data), "en")
