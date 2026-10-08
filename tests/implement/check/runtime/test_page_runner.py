from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from tightrein.implement.check.runtime import page_runner
from tightrein.implement.check.runtime.page_runner import Account, PageSettings, Results
from tightrein.protocol.process import Command, Outcome
from tightrein.protocol.security import Redactor

SETTINGS = PageSettings(roles=("anonymous", "admin"), locale="ja-JP", retries=2, timeout_s=60, patrol_grep="@patrol",
                        ignore_requests=({"method": "GET", "pathPattern": "/favicon", "status": 404},), routes=None)
SECRETS = {"pages.admin.account": "admin@example.com", "pages.admin.password": "s3cret-pass"}


def case(title: str, outcome: str, **extra: Any) -> dict[str, Any]:
    return {"title": title, "file": extra.pop("file", "common/orders.spec.ts"), "project": extra.pop("project",
                                                                                                     "patrol-admin"),
            "role": extra.pop("role", "admin"), "outcome": outcome, **extra}


def write_results(directory: Path, cases: list[dict[str, Any]], tail: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(item) + "\n" for item in cases) + tail
    (directory / page_runner.RESULTS_FILE).write_text(text)


class Playwright:
    """假 Playwright：记下命令，并按预置写出结果文件；还能看到登录态目录是否存在。"""

    def __init__(self, raw: Path, cases: list[dict[str, Any]], *, stopped: str | None = None,
                 tail: str = "") -> None:
        self.raw, self.cases, self.stopped, self.tail = raw, cases, stopped, tail
        self.commands: list[Command] = []
        self.plan: dict[str, Any] = {}
        self.auth_mode: int | None = None

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        self.plan = json.loads(Path(command.env[page_runner.PLAN_ENV]).read_text())
        state = next((item["storageState"] for item in self.plan["projects"] if "storageState" in item), None)
        if state is not None:
            self.auth_mode = stat.S_IMODE(Path(state).parent.stat().st_mode)
        write_results(self.raw, self.cases, self.tail)
        return Outcome(1, "", "", 5, self.stopped, None)


def _run(tmp_path: Path, fake: Playwright, secrets: dict[str, str] = SECRETS) -> page_runner.PageRun:
    runtime_dir = tmp_path / "pw"
    (runtime_dir / page_runner.PLAYWRIGHT_PACKAGE).parent.mkdir(parents=True)
    (runtime_dir / page_runner.PLAYWRIGHT_PACKAGE).write_text("{}")
    redactor = Redactor()
    redactor.register("s3cret-pass")
    return page_runner.run(base_url="http://localhost:13000", settings=SETTINGS, secrets=secrets,
                           e2e_dir=tmp_path / "e2e", raw_dir=fake.raw, runner=fake, environ={"PATH": "/usr/bin"},
                           redactor=redactor, runtime_dir=runtime_dir, temp_root=tmp_path)


def test_command_plan_and_login_state(tmp_path: Path) -> None:
    fake = Playwright(tmp_path / "raw", [case("订单列表", "expected")])
    run = _run(tmp_path, fake)
    assert run.status == page_runner.OK
    command = fake.commands[0]
    assert command.argv == ("npx", "playwright", "test", "--config", "playwright.config.ts", "--project",
                            "patrol-anonymous", "--project", "patrol-admin", "--grep", "@patrol")
    # 密码只经环境变量，计划文件只写账号名；登录态目录 0700，用完即删
    assert command.env["TIGHTREIN_PASSWORD_admin"] == "s3cret-pass"
    assert "s3cret-pass" not in json.dumps(fake.plan)
    setup = next(item for item in fake.plan["projects"] if item["kind"] == "setup")
    assert setup["account"] == "admin@example.com" and fake.auth_mode == 0o700
    assert not Path(setup["storageState"]).parent.exists()
    assert fake.plan["locale"] == "ja-JP" and fake.plan["ignoreRequests"][0]["pathPattern"] == "/favicon"
    anonymous = next(item for item in fake.plan["projects"] if item["name"] == "patrol-anonymous")
    assert "storageState" not in anonymous


def test_missing_or_incomplete_results_fail(tmp_path: Path) -> None:
    run = _run(tmp_path, Playwright(tmp_path / "raw", [case("a", "expected")], tail='{"title": "半行'))
    assert run.status == page_runner.FAILED and page_runner.INCOMPLETE in run.notes
    assert page_runner.parse(tmp_path / "nothing.ndjson") == Results((), False)


def test_timeout_keeps_what_was_written(tmp_path: Path) -> None:
    run = _run(tmp_path, Playwright(tmp_path / "raw", [case("a", "expected")], stopped="timeout",
                                    tail='{"title": "半行'))
    assert run.status == page_runner.PARTIAL and any("超时" in note for note in run.notes)


def test_unavailable_accounts_and_failed_logins(tmp_path: Path) -> None:
    run = _run(tmp_path, Playwright(tmp_path / "raw", [case("a", "expected", role="anonymous",
                                                            project="patrol-anonymous")]), secrets={})
    assert run.status == page_runner.PARTIAL and any("pages.admin.account" in note for note in run.notes)
    login = Playwright(tmp_path / "raw2", [case("登录", "unexpected", project="setup-admin", error="密码错误"),
                                           case("a", "expected", role="anonymous", project="patrol-anonymous")])
    assert any("登录失败：密码错误" in note for note in _run(tmp_path / "x", login).notes)
    found, missing = page_runner.accounts(["anonymous", "admin"], SECRETS)
    assert found == [Account("anonymous", None, None), Account("admin", "admin@example.com", "s3cret-pass")]
    assert missing == {}


def test_skipped_without_a_target_and_not_installed(tmp_path: Path) -> None:
    common = {"settings": SETTINGS, "secrets": SECRETS, "e2e_dir": tmp_path, "raw_dir": tmp_path / "raw",
                  "runner": Playwright(tmp_path, []), "environ": {}, "redactor": Redactor()}
    assert page_runner.run(base_url=None, **common).status == page_runner.SKIPPED
    not_installed = page_runner.run(base_url="http://x", runtime_dir=tmp_path / "empty", **common)
    assert not_installed.status == page_runner.FAILED and "npm ci" in not_installed.notes[0]


def test_nothing_ran_fails(tmp_path: Path) -> None:
    run = _run(tmp_path, Playwright(tmp_path / "raw", [case("a", "skipped")]))
    assert run.status == page_runner.FAILED and "没有任何角色的用例得到执行" in run.notes


def test_failures_per_case_and_page(tmp_path: Path) -> None:
    observation = {"pages": ["http://localhost:13000/orders?page=2"],
                   "consoleErrors": [{"pageUrl": "http://localhost:13000/orders", "text": "TypeError: x"},
                                     {"pageUrl": "http://localhost:13000/orders", "text": "TypeError: x"}],
                   "failedRequests": [{"pageUrl": "http://localhost:13000/orders", "method": "get",
                                       "url": "http://localhost:13000/api/orders?x=1", "status": 500}]}
    write_results(tmp_path, [case("列表", "unexpected", error="\x1b[31m没有数据\x1b[0m",
                                  attachments={"observations": [observation]}),
                             case("通过的也记控制台报错", "expected", attachments={"observations": [observation]}),
                             case("不稳定", "flaky")])
    results = page_runner.parse(tmp_path / page_runner.RESULTS_FILE)
    assert results.complete and len(results.cases) == 3
    found = page_runner.collect(results)
    kinds = sorted((item.case, item.kind, item.page, item.message) for item in found.failures)
    assert ("列表", "case-failure", "/orders", "没有数据") in kinds
    assert ("列表", "console-error", "/orders", "TypeError: x") in kinds
    assert ("列表", "failed-request", "/orders", "500 GET /api/orders") in kinds
    assert ("通过的也记控制台报错", "console-error", "/orders", "TypeError: x") in kinds
    assert len([item for item in kinds if item[0] == "列表" and item[1] == "console-error"]) == 1
    assert found.executed_roles == frozenset({"admin"})


def test_a_page_case_matches_its_spec_file_by_whole_name() -> None:
    assert page_runner.same_file("/ws/e2e/admin/page-1.spec.ts", "admin/page-1.spec.ts")
    assert page_runner.same_file("admin/page-1.spec.ts", "admin/page-1.spec.ts")
    assert not page_runner.same_file("/ws/e2e/admin/other-page-1.spec.ts", "page-1.spec.ts")
    assert page_runner.page_path("about:blank") is None and page_runner.page_path("https://x/a?b=1") == "/a"
