"""调用 Playwright 测试运行器(architecture/04 3.3)：在 runtime/ 目录下执行

    npx playwright test --config playwright.config.ts --project <项目> ... [--grep <正则>] [--grep-invert <正则>]

子进程环境只含白名单变量、计划文件路径与各角色的密码(TIGHTREIN_PASSWORD_<角色>)。用例失败时 Playwright 以非 0
退出，这属于正常结果，以结果文件为准；超过 checks.pages.timeoutMinutes 时终止进程组，已写出的结果照常解析。
Playwright 装在 runtime/node_modules/ 下，缺失时提示安装命令。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.sources.common.procs import Launcher, ToolCommand, ToolRun, tool_env

RUNTIME_DIR = Path(__file__).parent / "runtime"
CONFIG_FILE = "playwright.config.ts"
PLAN_ENV = "TIGHTREIN_PAGE_PLAN"
PASSWORD_ENV_PREFIX = "TIGHTREIN_PASSWORD_"
PLAYWRIGHT_PACKAGE = Path("node_modules") / "@playwright" / "test" / "package.json"
LOG_FILE = "playwright.log"
SECONDS_PER_MINUTE = 60
NPX = "npx"


def timeout_seconds(config: ProjectConfig) -> float:
    return float(config.get("checks.pages.timeoutMinutes")) * SECONDS_PER_MINUTE


def installed_problem(runtime: Path = RUNTIME_DIR) -> str | None:
    if (runtime / PLAYWRIGHT_PACKAGE).is_file():
        return None
    return f"Playwright 未安装：在 {runtime} 执行 npm ci 与 npx playwright install chromium"


def build_argv(projects: Sequence[str], grep: str | None, grep_invert: str | None) -> tuple[str, ...]:
    argv = [NPX, "playwright", "test", "--config", CONFIG_FILE]
    for project in projects:
        argv += ["--project", project]
    if grep is not None:
        argv += ["--grep", grep]
    if grep_invert is not None:
        argv += ["--grep-invert", grep_invert]
    return tuple(argv)


def run_env(environ: Mapping[str, str], plan_path: Path, passwords: Mapping[str, str]) -> dict[str, str]:
    extra = {PLAN_ENV: str(plan_path)}
    extra.update({f"{PASSWORD_ENV_PREFIX}{role}": password for role, password in passwords.items()})
    return tool_env(environ, extra)


def run(launcher: Launcher, plan_path: Path, projects: Sequence[str], grep: str | None, grep_invert: str | None,
        passwords: Mapping[str, str], environ: Mapping[str, str], timeout: float, log_file: Path,
        runtime: Path = RUNTIME_DIR) -> ToolRun:
    return launcher(ToolCommand(build_argv(projects, grep, grep_invert), runtime, timeout,
                                run_env(environ, plan_path, passwords), log_file))
