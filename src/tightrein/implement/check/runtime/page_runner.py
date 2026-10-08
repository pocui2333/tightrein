"""页面巡检的 Playwright 运行器：计划文件、调用、结果解析与页面上的失败。

Node 侧在同目录的 `playwright/`(配置、登录、页面观察 fixture、结果 reporter)，从环境变量 TIGHTREIN_PAGE_PLAN 指向的
计划文件读取全部参数，本身不含项目取值。步骤：检查安装 → 取各角色的账号 → 在新建的 0700 临时目录放登录态 → 写计划
文件 → 调用 Playwright → 删除登录态目录 → 解析 results.ndjson → 清除 trace 中的凭证 → 整理页面上的失败。

- 密码只经环境变量 TIGHTREIN_PASSWORD_<角色> 传入，计划文件只写账号名；匿名身份(anonymous)没有 setup 项目；
- 结果文件缺失或最后一行不完整为失败；超时终止后已写出的结果照常解析，状态为部分完成；
- 取不到账号的角色与登录失败的角色写入说明；没有任何用例执行为失败；没有目标地址为跳过；
- 用例失败时 Playwright 以非 0 退出，这属于正常结果，以结果文件为准。
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tightrein.implement.check.runtime import traces
from tightrein.protocol.naming import parse_duration
from tightrein.protocol.process import Command, ProcessRunner
from tightrein.protocol.security import Redactor, child_env
from tightrein.settings.load import Settings
from tightrein.store.files.json import write_json

RUNTIME_DIR = Path(__file__).with_name("playwright")
CONFIG_FILE = "playwright.config.ts"
PLAN_ENV = "TIGHTREIN_PAGE_PLAN"
PASSWORD_ENV_PREFIX = "TIGHTREIN_PASSWORD_"
PLAYWRIGHT_PACKAGE = Path("node_modules") / "@playwright" / "test" / "package.json"
NPX = "npx"
LOG_FILE = "playwright.log"
PLAN_FILE = "page-plan.json"
RESULTS_FILE = "results.ndjson"
HTML_DIR = "html"
OUTPUT_DIR = "test-results"
LOGIN_MODULE = "login.ts"
COMMON_DIR = "common"
SPEC_GLOB = "**/*.spec.ts"
SETUP_PREFIX = "setup-"
PATROL_PREFIX = "patrol-"
ANONYMOUS = "anonymous"
AUTH_DIR_PREFIX = "tightrein-pages-auth-"
AUTH_DIR_MODE = 0o700
TIMEOUT_STOPS = frozenset({"timeout", "idle"})
OUTCOMES = frozenset({"expected", "unexpected", "flaky", "skipped"})
WEB_SCHEMES = ("http", "https")
ROOT = "/"
CASE_FAILURE = "case-failure"
CONSOLE_ERROR = "console-error"
FAILED_REQUEST = "failed-request"
INCOMPLETE = "结果不完整：results.ndjson 缺失或最后一行不完整"
NO_TARGET = "没有页面服务的地址"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

OK = "ok"
PARTIAL = "partial"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class PageSettings:
    roles: tuple[str, ...]
    locale: str
    retries: int
    timeout_s: float
    patrol_grep: str | None
    ignore_requests: tuple[Mapping[str, Any], ...]
    routes: str | None

    @classmethod
    def from_settings(cls, settings: Settings) -> PageSettings:
        section = settings.section("implement.check.runtime")["pages"]
        return cls(tuple(section["roles"]), section["locale"], int(section["retries"]),
                   parse_duration(section["timeout"]), section["patrolGrep"], tuple(section["ignoreRequests"]),
                   section["routes"])


@dataclass(frozen=True)
class CaseResult:
    title: str
    file: str
    project: str
    role: str
    outcome: str
    failed_step: str | None
    error: str | None
    screenshots: tuple[str, ...] = ()
    observations: tuple[Mapping[str, Any], ...] = ()

    @property
    def setup(self) -> bool:
        return self.project.startswith(SETUP_PREFIX)


@dataclass(frozen=True)
class Results:
    cases: tuple[CaseResult, ...]
    complete: bool
    unknown: int = 0


@dataclass(frozen=True)
class PageFailure:
    page: str
    kind: str
    message: str
    case: str


@dataclass(frozen=True)
class PageFailures:
    failures: tuple[PageFailure, ...]
    failed_setups: Mapping[str, str] = field(default_factory=dict)
    executed_roles: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PageRun:
    status: str  # ok、partial、failed、skipped
    failures: tuple[PageFailure, ...] = ()
    cases: tuple[CaseResult, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Account:
    role: str
    name: str | None  # 匿名身份为 None
    password: str | None


def installed_problem(runtime_dir: Path = RUNTIME_DIR) -> str | None:
    if (runtime_dir / PLAYWRIGHT_PACKAGE).is_file():
        return None
    return f"Playwright 未安装：在 {runtime_dir} 执行 npm ci 与 npx playwright install chromium"


def accounts(roles: Sequence[str], secrets: Mapping[str, str]) -> tuple[list[Account], dict[str, str]]:
    """各角色的账号(secrets.json 的 `pages.<角色>.account` 与 `.password`)与取不到的原因。"""
    found: list[Account] = []
    missing: dict[str, str] = {}
    for role in roles:
        if role == ANONYMOUS:
            found.append(Account(role, None, None))
            continue
        name, password = secrets.get(f"pages.{role}.account"), secrets.get(f"pages.{role}.password")
        if name and password:
            found.append(Account(role, name, password))
        else:
            missing[role] = f"secrets.json 中没有 pages.{role}.account 或 pages.{role}.password"
    return found, missing


def build_plan(*, base_url: str, settings: PageSettings, accounts: Sequence[Account], e2e_dir: Path, auth_dir: Path,
               raw_dir: Path) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    for account in accounts:
        state: dict[str, str] = {}
        if account.name is not None:
            state = {"storageState": str(auth_dir / f"{account.role}.json")}
            projects.append({"name": f"{SETUP_PREFIX}{account.role}", "kind": "setup", "role": account.role,
                             "account": account.name, **state})
        projects.append({"name": f"{PATROL_PREFIX}{account.role}", "kind": "patrol", "role": account.role, **state,
                         "testDir": str(e2e_dir),
                         "testMatch": [f"{account.role}/{SPEC_GLOB}", f"{COMMON_DIR}/{SPEC_GLOB}"]})
    return {
        "baseURL": base_url,
        "locale": settings.locale,
        "retries": settings.retries,
        "outputDir": str(raw_dir / OUTPUT_DIR),
        "htmlDir": str(raw_dir / HTML_DIR),
        "resultsFile": str(raw_dir / RESULTS_FILE),
        "loginModule": str(e2e_dir / LOGIN_MODULE),
        "ignoreRequests": [dict(item) for item in settings.ignore_requests],
        "projects": projects,
    }


def build_argv(plan: Mapping[str, Any], grep: str | None) -> tuple[str, ...]:
    """传给 --project 的只有巡检项目，setup 项目作为依赖自动运行。"""
    argv = [NPX, "playwright", "test", "--config", CONFIG_FILE]
    for project in plan["projects"]:
        if project["kind"] != "setup":
            argv += ["--project", project["name"]]
    if grep is not None:
        argv += ["--grep", grep]
    return tuple(argv)


def run_env(environ: Mapping[str, str], plan_path: Path, accounts: Sequence[Account]) -> dict[str, str]:
    values = {PLAN_ENV: str(plan_path)}
    values.update({f"{PASSWORD_ENV_PREFIX}{item.role}": item.password for item in accounts if item.password})
    return child_env(environ, set_values=values)


def run(*, base_url: str | None, settings: PageSettings, secrets: Mapping[str, str], e2e_dir: Path, raw_dir: Path,
        runner: ProcessRunner, environ: Mapping[str, str], redactor: Redactor, runtime_dir: Path = RUNTIME_DIR,
        temp_root: Path | None = None) -> PageRun:
    if base_url is None:
        return PageRun(SKIPPED, notes=(NO_TARGET,))
    problem = installed_problem(runtime_dir)
    if problem is not None:
        return PageRun(FAILED, notes=(problem,))
    found, missing = accounts(settings.roles, secrets)
    notes = [f"角色 {role}：{reason}" for role, reason in missing.items()]
    if not found:
        return PageRun(FAILED, notes=(*notes, "全部角色的账号都不可用"))
    raw_dir.mkdir(parents=True, exist_ok=True)
    auth_dir = Path(tempfile.mkdtemp(prefix=AUTH_DIR_PREFIX, dir=temp_root))
    auth_dir.chmod(AUTH_DIR_MODE)
    try:
        plan = build_plan(base_url=base_url, settings=settings, accounts=found, e2e_dir=e2e_dir, auth_dir=auth_dir,
                          raw_dir=raw_dir)
        plan_path = raw_dir / PLAN_FILE
        write_json(plan_path, plan)
        outcome = runner.run(Command(build_argv(plan, settings.patrol_grep), runtime_dir,
                                     run_env(environ, plan_path, found), timeout_s=settings.timeout_s,
                                     stdout_path=raw_dir / LOG_FILE))
    finally:
        shutil.rmtree(auth_dir, ignore_errors=True)
    if outcome.start_error is not None:
        return PageRun(FAILED, notes=(*notes, f"Playwright 无法启动：{outcome.start_error}"))
    return judge(raw_dir, timed_out=outcome.stopped_by in TIMEOUT_STOPS, degraded=bool(missing), notes=notes,
                 redactor=redactor)


def judge(raw_dir: Path, *, timed_out: bool, degraded: bool, notes: list[str], redactor: Redactor) -> PageRun:
    results = parse(raw_dir / RESULTS_FILE)
    if not results.complete and not timed_out:
        return PageRun(FAILED, notes=(*notes, INCOMPLETE))
    notes = notes + traces.clean_all(raw_dir, redactor)
    found = collect(results)
    notes += [f"角色 {role} 登录失败：{error}" for role, error in found.failed_setups.items()]
    if timed_out:
        notes.append("Playwright 超时被终止，已写出的结果照常解析")
    if results.unknown:
        notes.append(f"结果文件中有 {results.unknown} 行无法解析，已跳过")
    if not found.executed_roles:
        status = FAILED
        notes.append("没有任何角色的用例得到执行")
    elif degraded or found.failed_setups or timed_out:
        status = PARTIAL
    else:
        status = OK
    cleaned = tuple(PageFailure(item.page, item.kind, redactor.text(item.message), item.case)
                    for item in found.failures)
    return PageRun(status, cleaned, results.cases, tuple(notes))


def parse(path: Path) -> Results:
    """reporter 每行写一条用例在最终一次尝试后的结果。逐行读，不整体读进内存。"""
    if not path.is_file():
        return Results((), False)
    cases: list[CaseResult] = []
    unknown = 0
    last_ok = False
    seen = False
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if not line.strip():
                continue
            seen = True
            try:
                cases.append(_case(json.loads(line)))
                last_ok = True
            except (ValueError, KeyError, TypeError, AttributeError):
                unknown += 1
                last_ok = False
    return Results(tuple(cases), seen and last_ok, unknown)


def collect(results: Results) -> PageFailures:
    """用例失败记在失败时所在的页面；控制台报错与失败请求不论用例是否通过都记；同一用例、同一页面、同一原文只记一次；
    setup 用例不产出失败，失败的角色单列；flaky 不算失败。"""
    found: dict[tuple[str, str, str, str], PageFailure] = {}
    failed_setups: dict[str, str] = {}
    executed: set[str] = set()

    def add(case: CaseResult, page: str, kind: str, message: str) -> None:
        found.setdefault((case.title, page, kind, message), PageFailure(page, kind, message, case.title))

    for case in results.cases:
        if case.setup:
            if case.outcome == "unexpected":
                failed_setups[case.role] = case.error or "登录失败"
            continue
        if case.outcome == "skipped":
            continue
        executed.add(case.role)
        if case.outcome == "unexpected":
            add(case, _last_page(case), CASE_FAILURE, case.error or f"用例失败：{case.title}")
        for observation in case.observations:
            for item in observation.get("consoleErrors") or []:
                add(case, page_path(item.get("pageUrl") or "") or ROOT, CONSOLE_ERROR, item.get("text") or "控制台报错")
            for item in observation.get("failedRequests") or []:
                method = (item.get("method") or "GET").upper()
                status = str(item["status"]) if item.get("status") is not None else (item.get("failure") or "请求失败")
                add(case, page_path(item.get("pageUrl") or "") or ROOT, FAILED_REQUEST,
                    f"{status} {method} {urlsplit(item.get('url') or '').path}")
    return PageFailures(tuple(found.values()), failed_setups, frozenset(executed))


def page_path(url: str) -> str | None:
    """页面 URL 的路径；about:blank 等非网页地址为 None。"""
    parts = urlsplit(url)
    return (parts.path or ROOT) if parts.scheme in WEB_SCHEMES else None


def same_file(reported: str, spec: str) -> bool:
    """结果中的文件路径(可能是绝对路径)是否就是这个用例文件：按完整的路径段比较，other-page-1.spec.ts 不算
    page-1.spec.ts。"""
    return reported == spec or reported.endswith("/" + spec)


def _case(data: Mapping[str, Any]) -> CaseResult:
    outcome = data["outcome"]
    if outcome not in OUTCOMES:
        raise ValueError(f"未知的 outcome：{outcome}")
    attachments = data.get("attachments") or {}
    error = data.get("error")
    return CaseResult(
        title=data["title"], file=data["file"], project=data["project"], role=data["role"], outcome=outcome,
        failed_step=data.get("failedStep"), error=None if error is None else ANSI_ESCAPE.sub("", error),
        screenshots=tuple(attachments.get("screenshot") or ()),
        observations=tuple(attachments.get("observations") or ()),
    )


def _last_page(case: CaseResult) -> str:
    for observation in reversed(case.observations):
        for url in reversed(observation.get("pages") or []):
            path = page_path(url)
            if path is not None:
                return path
    return ROOT
