"""接入(redesign/10-onboarding.md)：清单的自动完成、需要回答与失败，回答写回 project.yaml，全部完成后转为运行中。"""

from types import SimpleNamespace

import yaml
from pipeline_world import NOW, make_world

from tightrein.config import project
from tightrein.orchestrator.onboarding import service
from tightrein.orchestrator.onboarding.service import Onboarding, OnboardingDeps
from tightrein.store.files import documents
from tightrein.vcs.parse import Commit


class Git:
    def log(self, repo, ref, limit=None):
        return [Commit("c" * 40, "dev", NOW, f"feat: change {number}") for number in range(12)]

    def remote_branches(self, repo):
        return [f"feature/{number}-x" for number in range(12)]


def workspace(tmp_path, **sections):
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (repo / ".github" / "workflows" / "deploy.yml").write_text("on: push\n", encoding="utf-8")
    world = make_world(tmp_path)
    data = {"project": {"name": "demo", "repo": str(repo), "mainBranch": "main", "language": "zh"}, **sections}
    path = world.layout.project_config()
    path.write_text("# 演示\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    failures = []
    flow = Onboarding(OnboardingDeps(world.conn, world.layout, world.clock, lambda: project.load(path), git=Git(),
                                     try_point=lambda config, point: SimpleNamespace(failure=None),
                                     run_checks=lambda config: list(failures), health=lambda config: None,
                                     account=lambda item: None))
    return world, flow, failures


def states(report):
    return {item.item: item.state for item in report.items}


def test_a_new_workspace_lists_what_is_done_and_what_needs_an_answer(tmp_path):
    world, flow, _ = workspace(tmp_path)
    flow.start()
    report = flow.check()
    assert states(report) == {"stack": "done", "checks": "blocked", "conventions": "blocked",
                              "platform:deploy-source": "blocked", "platform:error-tracking": "blocked",
                              "platform:log-platform": "blocked", "platform:alert-source": "blocked"}
    assert report.phase == service.ONBOARDING and report.remaining == 6
    assert flow.status_line() == "demo：接入中，还差 6 项需要回答"
    titles = {item.item: item.title for item in report.items}
    assert (titles["platform:error-tracking"], titles["platform:log-platform"], titles["platform:alert-source"]) == (
        "平台接入：错误追踪", "平台接入：集中日志", "平台接入：业务告警")
    parsed = documents.read(world.layout.onboarding_document())
    assert parsed.header["kind"] == "progress" and parsed.header["status"] == "blocked"
    assert parsed.blocks["checklist"][1] == {"item": "[checks] 检查命令在基准版本上通过", "state": "blocked",
                                            "owner": "user"}
    conventions = next(item for item in report.items if item.item == "conventions")
    assert "{prefix}{type}/{issue}-{slug}" in conventions.recommendation


def test_answers_are_written_back_and_the_workspace_starts_running(tmp_path):
    world, flow, failures = workspace(tmp_path)
    flow.start()
    flow.check()
    flow.answer("conventions", service.RECOMMENDED)
    flow.answer("platform:deploy-source", service.RECOMMENDED)
    for point in ("error-tracking", "log-platform", "alert-source"):
        flow.answer(f"platform:{point}", service.SKIPPED)
    failures.append("pytest(退出码 1)")
    report = flow.answer("checks", service.VALUE, "python -m pytest -q")
    data = yaml.safe_load(world.layout.project_config().read_text(encoding="utf-8"))
    assert data["git"]["conventions"]["branch"] == "{prefix}{type}/{issue}-{slug}" and "commit" in data["git"]["conventions"]
    assert world.layout.project_config().read_text(encoding="utf-8").startswith("# 演示\n")
    assert data["extensions"]["deploy-source"] == {"use": "core/github-actions", "options": {"workflow": "deploy.yml"}}
    assert data["checks"]["commands"][0]["command"] == "python -m pytest -q"
    assert states(report)["checks"] == "failed" and report.phase == service.ONBOARDING
    failures.clear()
    report = flow.check()
    assert report.phase == service.RUNNING and report.remaining == 0 and flow.status_line() is None
    assert "转为运行中" in documents.read(world.layout.onboarding_document()).body


def test_marking_an_item_done_in_the_file_counts_as_an_answer(tmp_path):
    world, flow, _ = workspace(tmp_path)
    flow.start()
    flow.check()
    path = world.layout.onboarding_document()
    text = path.read_text(encoding="utf-8")
    marked = text.replace("- item: '[platform:alert-source] 平台接入：业务告警'\n  state: blocked",
                          "- item: '[platform:alert-source] 平台接入：业务告警'\n  state: done")
    assert marked != text
    path.write_text(marked, encoding="utf-8")
    assert states(flow.check())["platform:alert-source"] == "done"


def test_existing_workspaces_are_running_and_only_get_a_report(tmp_path):
    world, flow, _ = workspace(tmp_path)
    report = flow.check()
    assert report.phase == service.RUNNING and not flow.active() and report.remaining == 6


def test_without_an_exported_spec_the_answer_is_an_ai_draft_confirmed_by_the_user(tmp_path):
    world, flow, _ = workspace(tmp_path, target={"baseUrl": "https://demo.example.test"})
    flow.start()
    spec = next(item for item in flow.check().items if item.item == "spec")
    assert spec.state == "blocked" and "tightrein project spec draft" in spec.detail
    (world.layout.root / service.SPEC_DRAFT).write_text("openapi: 3.0.0\n", encoding="utf-8")
    spec = next(item for item in flow.check().items if item.item == "spec")
    assert f"{service.SPEC_DRAFT} 待确认" in spec.detail
    flow.answer("spec", service.RECOMMENDED)
    data = yaml.safe_load(world.layout.project_config().read_text(encoding="utf-8"))
    assert data["extensions"]["spec-export"] == {"use": "core/openapi-file",
                                                 "options": {"path": service.SPEC_CONFIRMED, "base": "workspace"}}
