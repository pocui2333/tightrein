import sys
from datetime import timedelta

import pytest
from probe_world import NOW

from tightrein.domain.enums import DeploymentStatus
from tightrein.observability.redact import Redactor
from tightrein.sources.common.procs import SubprocessLauncher, ToolCommand, tool_env
from tightrein.sources.common.routes import api_matcher, clean_path, page_matcher
from tightrein.sources.common.target import head_matches, latest_release, release_at
from tightrein.store.repos import deployments
from tightrein.store.repos.deployments import Deployment


def test_launcher_captures_output_and_writes_a_redacted_log(tmp_path):
    redactor = Redactor()
    redactor.register("hunter2")
    log = tmp_path / "logs" / "tool.log"
    script = "import sys; print('out hunter2'); sys.stderr.write('err'); sys.exit(3)"
    run = SubprocessLauncher(redactor=redactor)(ToolCommand((sys.executable, "-c", script), tmp_path, 30,
                                                            {"PATH": "/usr/bin:/bin"}, log))
    assert (run.exit_code, run.stdout, run.stderr, run.timed_out) == (3, "out hunter2\n", "err", False)
    text = log.read_text(encoding="utf-8")
    assert "# 退出码 3" in text and "hunter2" not in text and "[已脱敏]" in text


def test_launcher_timeout_and_missing_program(tmp_path):
    launcher = SubprocessLauncher()
    slow = launcher(ToolCommand((sys.executable, "-c", "import time; time.sleep(30)"), tmp_path, 0.5))
    assert slow.timed_out and slow.describe() == "超时，进程组已终止"
    missing = launcher(ToolCommand(("/nonexistent/tool",), tmp_path, 5))
    assert not missing.started and missing.exit_code is None and missing.describe().startswith("无法启动")


def test_tool_env_drops_credentials_and_keeps_proxies_and_extras():
    env = tool_env({"PATH": "/bin", "GITHUB_TOKEN": "x", "https_proxy": "http://127.0.0.1:8118", "EDITOR": "vi"},
                   {"TIGHTREIN_TOKEN": "t"})
    assert env["PATH"] == "/bin" and env["https_proxy"] == "http://127.0.0.1:8118"
    assert env["TIGHTREIN_TOKEN"] == "t"
    assert "GITHUB_TOKEN" not in env and "EDITOR" not in env


def test_clean_path():
    assert clean_path("https://h:8080/api/Order/7?x=1#top") == "/api/Order/7"
    assert clean_path("/newhome/CompanyList/") == "/newhome/CompanyList"
    assert clean_path("") == "/"


def test_api_templates_prefer_the_most_literal_match():
    matcher = api_matcher(["/api/Order/{id}", "/api/Order/List", "/api/{controller}/List", "/api/File/{name}.{ext}"])
    assert matcher.match("/api/Order/List") == "/api/Order/List"
    assert matcher.match("/api/order/42?x=1") == "/api/Order/{id}"
    assert matcher.match("/api/User/List") == "/api/{controller}/List"
    assert matcher.match("/api/File/a.pdf") == "/api/File/{name}.{ext}"
    assert matcher.match("/api/Order/7/Items") is None
    assert matcher.template_or_path("/api/Unknown?id=1") == "/api/Unknown"


def test_page_templates():
    matcher = page_matcher(["/newhome/CompanyDetail/:id", "/newhome/Edit/:filename?", "/newhome/CompanyList", "/",
                            "/docs/article/:slug(\\w+)", "/files/*"])
    assert matcher.match("/Newhome/CompanyDetail/12") == "/newhome/CompanyDetail/:id"
    assert matcher.match("/newhome/Edit") == "/newhome/Edit/:filename?"
    assert matcher.match("/newhome/Edit/a.dat") == "/newhome/Edit/:filename?"
    assert matcher.match("/") == "/"
    assert matcher.match("/docs/article/intro") == "/docs/article/:slug(\\w+)"
    assert matcher.match("/files/a/b/c") == "/files/*"
    assert matcher.match("/newhome/CompanyDetail/12/x") is None


def deployment(commit, status, hours, deployed=True):
    detected = NOW + timedelta(hours=hours)
    return Deployment(commit, status, detected, deployed_at=detected + timedelta(minutes=5) if deployed else None)


def test_releases_follow_successful_deployments(conn):
    deployments.save(conn, deployment("a" * 40, DeploymentStatus.SUCCEEDED, 0))
    deployments.save(conn, deployment("b" * 40, DeploymentStatus.FAILED, 1))
    deployments.save(conn, deployment("c" * 40, DeploymentStatus.SUCCEEDED, 2, deployed=False))
    assert latest_release(conn) == "c" * 40
    assert release_at(conn, NOW + timedelta(hours=1)) == "a" * 40
    assert release_at(conn, NOW + timedelta(hours=2)) == "c" * 40
    assert release_at(conn, NOW - timedelta(hours=1)) is None


def test_head_matches_accepts_a_prefix():
    assert head_matches("abc123", "abc") and not head_matches(None, "abc") and not head_matches("abc", "")


@pytest.mark.parametrize("head", ["b" * 40, None])
def test_head_mismatch(head):
    assert not head_matches(head, "a" * 40)
