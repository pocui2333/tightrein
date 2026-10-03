"""部署来源的三个核心方法：GitHub Actions、GitHub Deployments、Vercel(只读；gh、钥匙串与 HTTP 都是替身)。"""

import json
from pathlib import Path

from method_world import error_of, output_of, request

from tightrein.domain.enums import ExtensionErrorCode, ExtensionPoint
from tightrein.extensions.invoke import ProcessOutcome
from tightrein.extensions.methods.deploy_source import github_actions, github_deployments, vercel
from tightrein.extensions.methods.runtime import MethodContext
from tightrein.sources.common.http import HttpResponse

FIXTURES = Path(__file__).parent / "fixtures"
POINT = ExtensionPoint.DEPLOY_SOURCE
TOKEN = "vercel-token-0123456789abcdef"


class Runner:
    """按命令里的片段返回输出；secret 为 security 返回的令牌。"""

    def __init__(self, routes=(), secret=TOKEN):
        self.routes = routes
        self.secret = secret
        self.requests = []

    def __call__(self, sent):
        self.requests.append(sent)
        if sent.argv[0] == "security":
            if self.secret is None:
                return ProcessOutcome(44, b"", b"The specified item could not be found in the keychain.\n")
            return ProcessOutcome(0, f"{self.secret}\n".encode(), b"")
        for fragment, stdout in self.routes:
            if fragment in " ".join(sent.argv):
                return ProcessOutcome(0, stdout.encode("utf-8"), b"")
        return ProcessOutcome(1, b"", b"HTTP 404: Not Found\n")


def deploy_request(tmp_path, options):
    return request(POINT, workspace=tmp_path, repo=tmp_path, options=options, input={"branch": "main"})


def test_github_actions_runs_become_deployments(tmp_path):
    runner = Runner([("run list", (FIXTURES / "run-list-deploy.json").read_text(encoding="utf-8"))])
    output = output_of(github_actions, deploy_request(tmp_path, {"workflow": "deploy.yml"}),
                       MethodContext(runner=runner))
    assert runner.requests[0].argv == ("gh", "run", "list", "--workflow", "deploy.yml", "--branch", "main", "--json",
                                       "databaseId,headSha,status,conclusion,createdAt,url", "--limit", "20")
    assert [(item["id"], item["status"]) for item in output["deployments"]] == [
        ("101", "succeeded"), ("102", "skipped"), ("103", "running")]
    failed = Runner([("run list", (FIXTURES / "run-list-failed.json").read_text(encoding="utf-8"))])
    found = output_of(github_actions, deploy_request(tmp_path, {"workflow": "deploy.yml", "branch": "release"}),
                      MethodContext(runner=failed))
    assert found["deployments"][0]["status"] == "failed" and "--branch" in failed.requests[0].argv
    assert failed.requests[0].argv[failed.requests[0].argv.index("--branch") + 1] == "release"


def test_github_deployments_take_the_latest_status_of_each(tmp_path):
    listing = json.dumps([{"id": 7, "sha": "a" * 40, "environment": "staging", "created_at": "2026-09-29T01:00:00Z"},
                          {"id": 8, "sha": "b" * 40, "environment": "staging", "created_at": "2026-09-29T02:00:00Z"}])
    runner = Runner([("deployments?", listing),
                     ("deployments/7/statuses", json.dumps([{"state": "inactive", "environment_url": "https://s"}])),
                     ("deployments/8/statuses", json.dumps([{"state": "failure", "target_url": "https://ci/8"}]))])
    output = output_of(github_deployments, deploy_request(tmp_path, {"environment": "staging"}),
                       MethodContext(runner=runner))
    assert runner.requests[0].argv == ("gh", "api", "repos/{owner}/{repo}/deployments?per_page=20&environment=staging")
    assert [(item["id"], item["status"], item["url"]) for item in output["deployments"]] == [
        ("7", "succeeded", "https://s"), ("8", "failed", "https://ci/8")]
    broken = Runner([])
    code, message = error_of(github_deployments, deploy_request(tmp_path, {}), MethodContext(runner=broken))
    assert code == ExtensionErrorCode.SOURCE_UNAVAILABLE.value and "HTTP 404" in message


def test_vercel_reads_the_token_from_the_keychain_and_keeps_it_out_of_the_output(tmp_path):
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

    options = {"projectId": "prj_1", "keychainItem": "tightrein.demo.vercel", "teamId": "team_1"}
    runner = Runner()
    output = output_of(vercel, deploy_request(tmp_path, options), MethodContext(runner=runner, transport=transport))
    assert runner.requests[0].argv == ("security", "find-generic-password", "-s", "tightrein.demo.vercel", "-w")
    assert sent[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent[0].url == ("https://api.vercel.com/v6/deployments?projectId=prj_1&target=production&limit=20"
                           "&teamId=team_1")
    assert [(item["id"], item["status"], item["url"]) for item in output["deployments"]] == [
        ("dpl_1", "succeeded", "https://app-1.vercel.app"), ("dpl_2", "running", "https://app-2.vercel.app")]
    assert TOKEN not in json.dumps(output)
    code, message = error_of(vercel, deploy_request(tmp_path, options), MethodContext(runner=Runner(secret=None)))
    assert code == ExtensionErrorCode.SOURCE_UNAVAILABLE.value and "条目不存在" in message and TOKEN not in message
    code, message = error_of(vercel, deploy_request(tmp_path, options), MethodContext(
        runner=Runner(), transport=lambda request: HttpResponse(403, b"{}")))
    assert "403" in message
