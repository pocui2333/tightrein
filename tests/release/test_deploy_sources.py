"""部署记录的读取部分(release/deploy.py 与方法库 release/deploy_source/)：三种部署来源、按接入清单加载方法并按清单
校验参数与凭据、记进 state 表；gh 与 HTTP 都是替身，不联网。"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tightrein.collect.common.signals import DEPLOYMENTS_KEY, deployments, latest_release
from tightrein.onboard.setup import ModuleStatus
from tightrein.protocol.http import HttpResponse
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.process import Outcome
from tightrein.release import deploy
from tightrein.release.deploy import DeployError, DeployErrorKind, DeployRecord, Gh, remember
from tightrein.release.deploy_source.github_actions import github_actions
from tightrein.release.deploy_source.github_deployments import github_deployments
from tightrein.release.deploy_source.vercel import vercel
from tightrein.store.db import open_database
from tightrein.store.tables import state

TOKEN = "vercel-token-0123456789abcdef"


class Runner:
    """按命令里的片段返回输出。"""

    def __init__(self, routes=(), *, outcome=None):
        self.routes = routes
        self.outcome = outcome
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        if self.outcome is not None:
            return self.outcome
        for fragment, stdout in self.routes:
            if fragment in " ".join(command.argv):
                return Outcome(0, stdout, "", 5, None, None)
        return Outcome(1, "", "line 1\nline 2\nline 3\nHTTP 404: Not Found\n", 5, None, None)


def gh(runner):
    return Gh(runner, Path("/repo"), {}, 30.0)


def test_github_actions_runs_become_deployments():
    runs = [
        {"databaseId": 101, "headSha": "a" * 40, "status": "completed", "conclusion": "success",
         "createdAt": "2026-10-01T01:00:00Z", "url": "https://ci/101"},
        {"databaseId": 102, "headSha": "b" * 40, "status": "completed", "conclusion": "skipped",
         "createdAt": "2026-10-01T02:00:00Z", "url": None},
        {"databaseId": 103, "headSha": "c" * 40, "status": "in_progress", "conclusion": None,
         "createdAt": "2026-10-01T03:00:00Z", "url": None},
        {"databaseId": 104, "headSha": "d" * 40, "status": "completed", "conclusion": "cancelled",
         "createdAt": "2026-10-01T04:00:00Z", "url": None},
    ]
    runner = Runner([("run list", json.dumps(runs))])
    found = github_actions(gh(runner), workflow="deploy.yml", branch="main", limit=20)
    assert runner.commands[0].argv == ("gh", "run", "list", "--workflow", "deploy.yml", "--branch", "main", "--json",
                                       "databaseId,headSha,status,conclusion,createdAt,url", "--limit", "20")
    assert [(item.id, item.status) for item in found] == [
        ("101", "succeeded"), ("102", "skipped"), ("103", "running"), ("104", "failed")]


def test_github_deployments_take_the_latest_status_of_each():
    listing = json.dumps([{"id": 7, "sha": "a" * 40, "environment": "staging", "created_at": "2026-09-29T01:00:00Z"},
                          {"id": 8, "sha": "b" * 40, "environment": "staging", "created_at": "2026-09-29T02:00:00Z"},
                          {"id": 9, "sha": "c" * 40, "environment": "staging", "created_at": "2026-09-29T03:00:00Z"}])
    runner = Runner([("deployments?", listing),
                     ("deployments/7/statuses", json.dumps([{"state": "inactive", "environment_url": "https://s"}])),
                     ("deployments/8/statuses", json.dumps([{"state": "failure", "target_url": "https://ci/8"}])),
                     ("deployments/9/statuses", "[]")])
    found = github_deployments(gh(runner), environment="staging", limit=20)
    assert runner.commands[0].argv == ("gh", "api", "repos/{owner}/{repo}/deployments?per_page=20&environment=staging")
    assert [(item.id, item.status, item.url) for item in found] == [
        ("7", "succeeded", "https://s"), ("8", "failed", "https://ci/8"), ("9", "running", None)]


def test_gh_failures_are_told_apart_and_keep_only_the_last_three_stderr_lines():
    with pytest.raises(DeployError) as missing:
        github_deployments(gh(Runner(outcome=Outcome(None, "", "", 0, None, "No such file: gh"))), environment=None,
                           limit=5)
    assert missing.value.kind is DeployErrorKind.TOOL_MISSING
    with pytest.raises(DeployError) as slow:
        github_deployments(gh(Runner(outcome=Outcome(None, "", "", 0, "timeout", None))), environment=None, limit=5)
    assert slow.value.kind is DeployErrorKind.UNAVAILABLE and "30" in slow.value.message
    with pytest.raises(DeployError) as failed:
        github_deployments(gh(Runner()), environment=None, limit=5)
    assert failed.value.kind is DeployErrorKind.UNAVAILABLE
    assert "HTTP 404" in failed.value.message and "line 1" not in failed.value.message
    with pytest.raises(DeployError) as invalid:
        github_deployments(gh(Runner([("deployments?", "not json")])), environment=None, limit=5)
    assert invalid.value.kind is DeployErrorKind.INVALID


def test_vercel_skips_manual_uploads_and_keeps_the_token_in_the_header_only():
    sent = []

    def transport(request):
        sent.append(request)
        body = {"deployments": [
            {"uid": "dpl_2", "url": "app-2.vercel.app", "state": "BUILDING", "created": 1759633200000,
             "target": "production", "meta": {"githubCommitSha": "b" * 40}},
            {"uid": "dpl_1", "url": "app-1.vercel.app", "state": "READY", "created": 1759629600000,
             "target": "production", "meta": {"githubCommitSha": "a" * 40}},
            {"uid": "dpl_0", "url": "manual.vercel.app", "state": "READY", "created": 1759626000000, "meta": {}}]}
        return HttpResponse(200, json.dumps(body).encode("utf-8"))

    found = vercel(transport, token=TOKEN, project_id="prj_1", target="production", team_id="team_1", limit=20,
                   timeout_s=30)
    assert sent[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent[0].url == ("https://api.vercel.com/v6/deployments?projectId=prj_1&target=production&limit=20"
                           "&teamId=team_1")
    assert [(item.id, item.status, item.url) for item in found] == [
        ("dpl_1", "succeeded", "https://app-1.vercel.app"), ("dpl_2", "running", "https://app-2.vercel.app")]
    assert found[0].created_at == datetime(2025, 10, 5, 2, 0, tzinfo=UTC)
    assert TOKEN not in repr(found)
    with pytest.raises(DeployError) as denied:
        vercel(lambda request: HttpResponse(403, b"{}"), token=TOKEN, project_id="p", target="production",
               team_id=None, limit=1, timeout_s=1)
    assert "403" in denied.value.message and TOKEN not in denied.value.message


def test_remember_merges_by_deployment_and_keeps_the_first_detection(tmp_path):
    clock = FixedClock(datetime(2026, 10, 7, 9, 0, tzinfo=UTC))
    conn = open_database(tmp_path / "t.db", clock=clock)
    first = DeployRecord("1", "c1", "running", None, None, datetime(2026, 10, 7, 8, 0, tzinfo=UTC))
    remember(conn, [first], clock)
    clock.advance(datetime(2026, 10, 7, 10, 0, tzinfo=UTC) - clock.now())
    done = DeployRecord("1", "c1", "succeeded", None, None, datetime(2026, 10, 7, 8, 0, tzinfo=UTC))
    later = DeployRecord("2", "c2", "succeeded", None, None, datetime(2026, 10, 7, 9, 30, tzinfo=UTC))
    remember(conn, [later, done], clock)
    stored = state.get(conn, DEPLOYMENTS_KEY)
    assert [(item["id"], item["status"], item["detectedAt"]) for item in stored] == [
        ("1", "succeeded", "2026-10-07T09:00:00Z"), ("2", "succeeded", "2026-10-07T10:00:00Z")]
    assert latest_release(deployments(conn)) == "c2"
    conn.close()


def _vercel_runtime(kit, tmp_path, repos, secrets, **options):
    settings = kit.make_settings({"controls": {"release.deploy": {"vercel": {"projectId": "prj_1", **options}}}})
    runtime = kit.make_runtime(tmp_path, repos.repo, settings=settings,
                               setup=kit.make_setup(ModuleStatus.ENABLED, "vercel"))
    runtime.secrets = secrets
    return runtime


def test_the_source_is_a_method_whose_token_comes_from_the_manifest_entry(kit, tmp_path, repos):
    sent = []

    def transport(request):
        sent.append(request)
        body = {"deployments": [{"uid": "dpl_1", "url": "a.vercel.app", "state": "READY", "created": 1759629600000,
                                 "target": "production", "meta": {"githubCommitSha": "a" * 40}}]}
        return HttpResponse(200, json.dumps(body).encode("utf-8"))

    runtime = _vercel_runtime(kit, tmp_path, repos, {"vercel.token": TOKEN})
    found = deploy.recent(runtime, transport=transport)
    assert [(item.id, item.status) for item in found] == [("dpl_1", "succeeded")]
    assert sent[0].headers["Authorization"] == f"Bearer {TOKEN}" and "projectId=prj_1" in sent[0].url
    with pytest.raises(DeployError) as missing:
        deploy.recent(_vercel_runtime(kit, tmp_path / "b", repos, {"vercel": TOKEN}), transport=transport)
    assert missing.value.kind is DeployErrorKind.MISCONFIGURED and "vercel.token" in missing.value.message
    runtime.conn.close()


def test_parameters_are_checked_against_the_manifest_and_unknown_sources_are_refused(kit, tmp_path, repos):
    runtime = kit.make_runtime(tmp_path, repos.repo, setup=kit.make_setup(ModuleStatus.ENABLED, "github_actions"))
    with pytest.raises(DeployError) as unset:
        deploy.recent(runtime)  # 缺省 workflow 为 null：报缺少，不把 None 传给 gh
    assert unset.value.kind is DeployErrorKind.MISCONFIGURED and "workflow" in unset.value.message
    runtime.setup = kit.make_setup(ModuleStatus.ENABLED, "netlify")
    with pytest.raises(DeployError, match="不认识的部署来源：netlify"):
        deploy.recent(runtime)
    runtime.conn.close()


def test_github_actions_without_a_branch_uses_the_main_branch(kit, tmp_path, repos):
    settings = kit.make_settings({"controls": {"release.deploy": {"github_actions": {"workflow": "deploy.yml"}}}})
    runtime = kit.make_runtime(tmp_path, repos.repo, settings=settings,
                               setup=kit.make_setup(ModuleStatus.ENABLED, "github_actions"))
    runtime.runner.replies[("gh", "run", "list")] = [Outcome(0, "[]", "", 1, None, None)]
    assert deploy.recent(runtime) == []
    argv = runtime.runner.commands[-1].argv
    assert argv[argv.index("--branch") + 1] == "main" and argv[argv.index("--workflow") + 1] == "deploy.yml"
    runtime.conn.close()
