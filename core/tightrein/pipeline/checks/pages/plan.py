"""本次运行的 Playwright 计划文件(验证环节的页面巡检与页面类复现检查)。runtime/playwright.config.ts 从环境变量
TIGHTREIN_PAGE_PLAN 指向的这份文件读取全部参数，配置文件本身不含任何项目取值。

- 每个角色一个 setup-<角色> 项目(runtime/auth.setup.ts)，登录态写到本次运行新建的临时目录；匿名身份(账号为空)
  没有 setup 项目，其余项目不带 storageState、不依赖 setup；
- 巡检：patrol-<角色> 项目，testDir 为工作区 e2e/，只收 <角色>/ 与 common/ 下的用例；
- 指定了用例目录(spec_dirs，复现检查使用)时不跑巡检用例，每个目录每个角色一个 regress-<角色>[-<序号>] 项目；
- 公共参数：重试 2 次，第一次重试录制 trace，失败时保留截图与视频，baseURL 为目标地址，locale 取
  checks.pages.locale；输出目录、HTML 报告与 results.ndjson 都在本次运行的原始输出目录中；
- 登录页的操作由工作区 e2e/login.ts 导出的 login(page, account, password) 完成；账号名写入计划文件，
  密码只经环境变量 TIGHTREIN_PASSWORD_<角色> 传入；
- 巡检的 --grep 取 checks.pages.patrolGrep，调用方给出的 grep 优先。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig

LOGIN_MODULE = "login.ts"
COMMON_DIR = "common"
SPEC_GLOB = "**/*.spec.ts"
RESULTS_FILE = "results.ndjson"
HTML_DIR = "html"
OUTPUT_DIR = "test-results"
PLAN_FILE = "page-plan.json"
SETUP_PREFIX = "setup-"
PATROL_PREFIX = "patrol-"
REGRESS_PREFIX = "regress-"


def grep_for(config: ProjectConfig, grep: str | None) -> str | None:
    return grep if grep is not None else config.get("checks.pages.patrolGrep")


def storage_state(auth_dir: Path, role: str) -> Path:
    return auth_dir / f"{role}.json"


def build(*, base_url: str, locale: str, retries: int, accounts: Mapping[str, str | None], workspace_e2e: Path,
          auth_dir: Path, raw_dir: Path, ignore_requests: Sequence[Mapping[str, Any]] = (),
          spec_dirs: Sequence[Path] = ()) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    for role, account in accounts.items():
        state: dict[str, str] = {}
        if account is not None:
            state = {"storageState": str(storage_state(auth_dir, role))}
            projects.append({"name": f"{SETUP_PREFIX}{role}", "kind": "setup", "role": role, "account": account,
                             **state})
        if not spec_dirs:
            projects.append({"name": f"{PATROL_PREFIX}{role}", "kind": "patrol", "role": role, **state,
                             "testDir": str(workspace_e2e),
                             "testMatch": [f"{role}/{SPEC_GLOB}", f"{COMMON_DIR}/{SPEC_GLOB}"]})
        for number, directory in enumerate(spec_dirs, start=1):
            suffix = "" if len(spec_dirs) == 1 else f"-{number}"
            projects.append({"name": f"{REGRESS_PREFIX}{role}{suffix}", "kind": "regress", "role": role,
                             **state, "testDir": str(directory), "testMatch": [SPEC_GLOB]})
    return {
        "baseURL": base_url,
        "locale": locale,
        "retries": retries,
        "outputDir": str(raw_dir / OUTPUT_DIR),
        "htmlDir": str(raw_dir / HTML_DIR),
        "resultsFile": str(raw_dir / RESULTS_FILE),
        "loginModule": str(workspace_e2e / LOGIN_MODULE),
        "ignoreRequests": [dict(item) for item in ignore_requests],
        "projects": projects,
    }


def from_config(config: ProjectConfig, *, base_url: str, accounts: Mapping[str, str | None], workspace_e2e: Path,
                auth_dir: Path, raw_dir: Path, spec_dirs: Sequence[Path] = ()) -> dict[str, Any]:
    return build(base_url=base_url, locale=config.get("checks.pages.locale"),
                 retries=int(config.get("checks.pages.retries")), accounts=accounts, workspace_e2e=workspace_e2e,
                 auth_dir=auth_dir, raw_dir=raw_dir,
                 ignore_requests=config.get("checks.pages.ignoreRequests"), spec_dirs=spec_dirs)


def selected_projects(plan: Mapping[str, Any]) -> list[str]:
    """传给 --project 的项目：巡检与复现检查；setup 项目作为依赖自动运行。"""
    return [project["name"] for project in plan["projects"] if project["kind"] != "setup"]


def write(path: Path, plan: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
