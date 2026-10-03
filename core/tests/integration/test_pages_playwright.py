"""页面检查(验证环节的 Playwright 运行器)的端到端集成测试：本机静态页面服务模拟登录页与几个页面，运行真实的 Playwright。

runtime/node_modules 中没有 @playwright/test、本机没有 npx 或 Playwright 的 chromium 时跳过。
工作区的 login.ts、tsconfig.json 与用例在临时目录中生成：Company 的一条用例通过、一条在「查看统计卡片」步骤失败，
common/ 下一条用例第一次尝试失败、重试后通过(不稳定)；Admin 的密码错误，setup 失败。
"""

import base64
import json
import os
import shutil
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tightrein.domain.clock import SystemClock
from tightrein.domain.enums import RunStatus
from tightrein.observability.redact import Redactor
from tightrein.pipeline.checks.pages import invoke
from tightrein.pipeline.checks.pages.runner import PageDependencies, PageRunner
from tightrein.sources.base import ProbeTarget
from tightrein.sources.common.procs import SubprocessLauncher
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.store.files.layout import WorkspaceLayout

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LWNvbXBhbnkifQ.c2l0ZS1zaWduYXR1cmU"
PAGE = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title></head>'
        "<body>{body}</body></html>")
LOGIN_PAGE = PAGE.format(title="登录", body="""
<label>账号 <input id="user"></label><label>密码 <input id="pass" type="password"></label>
<button id="go">登录</button>
<script>
document.getElementById('go').onclick = async () => {
  const value = (id) => document.getElementById(id).value;
  const response = await fetch('/api/Auth/Login', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({userName: value('user'), password: value('pass')})});
  if (!response.ok) { document.body.insertAdjacentHTML('beforeend', '<p>登录失败</p>'); return; }
  localStorage.setItem('auth', btoa((await response.json()).data.token));
  location.href = '/home';
};
</script>""")
HOME_PAGE = PAGE.format(title="首页", body="""
<h1>首页</h1>
<script>console.error('首页组件加载失败'); fetch('/api/Stats');</script>""")
LIST_PAGE = PAGE.format(title="列表", body="<h1>列表</h1><table><tr><td>一</td></tr></table>")
PASSWORDS = {"test-company": "company-password", "test-admin": "admin-password"}

LOGIN_MODULE = """import type { Page } from '@playwright/test';

export async function login(page: Page, account: string, password: string): Promise<void> {
  await page.goto('/login');
  await page.locator('#user').fill(account);
  await page.locator('#pass').fill(password);
  await page.getByRole('button', { name: '登录' }).click();
  await page.waitForURL('**/home', { timeout: 5000 });
}
"""
TSCONFIG = {"compilerOptions": {"baseUrl": ".", "paths": {"tightrein/e2e": ["{runtime}/fixtures.ts"]}}}
LIST_SPEC = """import { test, expect } from 'tightrein/e2e';

test('列表可以打开', { tag: '@browse' }, async ({ page }) => {
  await page.goto('/list');
  await expect(page.getByRole('heading', { name: '列表' })).toBeVisible();
});
"""
BROKEN_SPEC = """import { test, expect } from 'tightrein/e2e';

test('首页有统计卡片', { tag: '@browse' }, async ({ page }) => {
  await test.step('打开首页', async () => {
    await page.goto('/home');
  });
  await test.step('查看统计卡片', async () => {
    await expect(page.getByText('统计卡片')).toBeVisible({ timeout: 1000 });
  });
});
"""
FLAKY_SPEC = """import { test, expect } from 'tightrein/e2e';

test('首页标题', { tag: '@browse' }, async ({ page }) => {
  await page.goto('/home');
  expect(test.info().retry).toBeGreaterThan(0);
  await expect(page.getByRole('heading', { name: '首页' })).toBeVisible();
});
"""
WRITE_SPEC = """import { test } from 'tightrein/e2e';

test('新建数据', { tag: '@write' }, async ({ page }) => {
  await page.goto('/home');
});
"""


def playwright_problem():
    if shutil.which("npx") is None:
        return "本机没有 npx"
    problem = invoke.installed_problem()
    if problem is not None:
        return problem
    check = subprocess.run(["npx", "playwright", "install", "--dry-run", "chromium"], cwd=invoke.RUNTIME_DIR,
                           capture_output=True, text=True, check=False)
    locations = [line.split(":", 1)[1].strip() for line in check.stdout.splitlines() if "Install location" in line]
    if check.returncode != 0 or not locations or not all(os.path.isdir(item) for item in locations):
        return "Playwright 的 chromium 未安装：在 runtime/ 执行 npx playwright install chromium"
    return None


pytestmark = pytest.mark.skipif(playwright_problem() is not None, reason=str(playwright_problem()))


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        pages = {"/login": LOGIN_PAGE, "/home": HOME_PAGE, "/list": LIST_PAGE}
        if self.path in pages:
            return self.send(200, pages[self.path])
        if self.path == "/api/Stats":
            return self.send(500, '{"error": "stats"}', "application/json")
        return self.send(404, "not found", "text/plain")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if PASSWORDS.get(body.get("userName")) == body.get("password") and body.get("userName") == "test-company":
            return self.send(200, json.dumps({"data": {"token": TOKEN}}), "application/json")
        return self.send(401, '{"error": "bad credentials"}', "application/json")


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class Credentials:
    accounts = {"Company": "test-company", "Admin": "test-admin"}

    def account(self, role):
        return self.accounts[role]

    def password(self, role):
        return "company-password" if role == "Company" else "wrong-password"


def write_workspace(layout):
    e2e = layout.e2e_dir()
    for relative, text in {"login.ts": LOGIN_MODULE, "Company/list.spec.ts": LIST_SPEC,
                           "Company/broken.spec.ts": BROKEN_SPEC, "common/home.spec.ts": FLAKY_SPEC,
                           "Company/write.spec.ts": WRITE_SPEC}.items():
        path = e2e / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    tsconfig = json.loads(json.dumps(TSCONFIG).replace("{runtime}", str(invoke.RUNTIME_DIR)))
    (e2e / "tsconfig.json").write_text(json.dumps(tsconfig), encoding="utf-8")


def test_real_playwright_run(tmp_path, site, make_config):
    layout = WorkspaceLayout(tmp_path / "workspace")
    write_workspace(layout)
    config = make_config(
        accounts={"roles": {"Company": {"keychain": "c"}, "Admin": {"keychain": "a"}},
                  "login": {"endpoint": "/api/Auth/Login", "bodyTemplate": {}, "tokenPath": "data.token"}},
        checks={"pages": {"patrolGrep": "@browse", "timeoutMinutes": 5}})
    redactor = Redactor()
    redactor.register(TOKEN)
    runner = PageRunner(PageDependencies(config, Credentials(), SubprocessLauncher(redactor=redactor), layout,
                                         ProbeRedactor(redactor)))
    raw_dir = tmp_path / "raw" / "pages"
    target = ProbeTarget("staging", "R-20261005-030000-verify", raw_dir, SystemClock(), base_url=site,
                         release="d6f37025", worktree=tmp_path)
    outcome = runner.run(target)
    assert outcome.status is RunStatus.PARTIAL, outcome.notes
    assert any("角色 Admin 登录失败" in note for note in outcome.notes)
    found = sorted((item.kind, item.page) for item in outcome.failures)
    assert ("case-failure", "/home") in found
    assert ("console-error", "/home") in found
    assert any(item.kind == "failed-request" and "/api/Stats" in item.message for item in outcome.failures)
    results = (raw_dir / "results.ndjson").read_text(encoding="utf-8")
    assert "新建数据" not in results
    traces = list(raw_dir.rglob("trace.zip"))
    assert traces
    encoded = base64.b64encode(TOKEN.encode())
    for path in traces:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                data = archive.read(name)
                assert TOKEN.encode() not in data and encoded not in data
                if name.endswith((".trace", ".network")):
                    assert all(json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip())
