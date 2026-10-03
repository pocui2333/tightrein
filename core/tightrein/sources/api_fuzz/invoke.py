"""按角色调用 Schemathesis CLI(architecture/04 2.4)。

参数的来源依次为档位缺省、project.yaml 的 sources.api-fuzz.levels.<档位>、ProbeOptions：

| 参数 | 浅跑 | 深跑 |
|---|---|---|
| --max-examples | 50 | 200 |
| --phases | examples,coverage,fuzzing | examples,coverage,fuzzing,stateful |
| --include-method | GET | 不限定 |

--exclude-path-regex 为 sources.api-fuzz.exclude 合并的正则(options 给出时以 options 为准)；关闭的检查项以
--exclude-checks 传入(api_fuzz/checks.py)；--max-response-time 只在响应过慢检查开启时取 thresholds.slowResponseSeconds；
生产环境的 --include-path 取允许清单(api_fuzz/limits.py)；--seed 每次运行随机生成，options 给出时复用。
报告写到 <角色目录>/events.ndjson、cassette.yaml、junit.xml。凭证只经环境变量 TIGHTREIN_TOKEN 传入(匿名身份不设置)；
有越权模型时另设 TIGHTREIN_AUTHZ_MODEL 与 TIGHTREIN_ROLE。退出码 0 与 1 都是正常完成，其余(含超时)为该角色失败。
Schemathesis 的版本须与锁定版本一致，否则不运行。
"""

from __future__ import annotations

import random
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from tightrein.config.project import MissingSetting, ProjectConfig
from tightrein.domain.enums import ProbeLevel
from tightrein.sources.api_fuzz import limits, spec
from tightrein.sources.api_fuzz.checks import CheckPlan
from tightrein.sources.api_fuzz.config_writer import TOKEN_ENV
from tightrein.sources.base import ProbeOptions
from tightrein.sources.common.procs import Launcher, ToolCommand, ToolRun, tool_env

SCHEMATHESIS_VERSION = "4.28.0"
SCHEMATHESIS_PACKAGE = "schemathesis"
MODEL_ENV = "TIGHTREIN_AUTHZ_MODEL"
ROLE_ENV = "TIGHTREIN_ROLE"
EVENTS_FILE = "events.ndjson"
CASSETTE_FILE = "cassette.yaml"
JUNIT_FILE = "junit.xml"
LOG_FILE = "schemathesis.log"
NORMAL_EXIT_CODES = (0, 1)
SECONDS_PER_MINUTE = 60
SEED_LIMIT = 2**31 - 1
LEVEL_KEYS = ("maxExamples", "phases", "includeMethod")

SeedSource = Callable[[], int]


def random_seed() -> int:
    return random.SystemRandom().randint(1, SEED_LIMIT)


def _setting(config: ProjectConfig, key: str, default: Any) -> Any:
    try:
        return config.get(key)
    except MissingSetting:
        return default


@dataclass(frozen=True)
class RunParameters:
    max_examples: int
    phases: tuple[str, ...]
    include_methods: tuple[str, ...]
    include_paths: tuple[str, ...]
    exclude_regex: str | None
    max_response_time: float | None
    seed: int
    workers: int
    sanitize_keys: tuple[str, ...]
    timeout_seconds: float
    excluded_checks: tuple[str, ...] = ()

    @property
    def methods(self) -> str:
        """coverage.methods：只测 GET 时为 GET，否则为 all。"""
        return "GET" if self.include_methods == ("GET",) else "all"


def parameters(level: ProbeLevel, config: ProjectConfig, options: ProbeOptions,
               seed_source: SeedSource = random_seed, plan: CheckPlan | None = None,
               allowed: tuple[str, ...] = ()) -> RunParameters:
    """plan 为检查项的开关(缺省为全部开启)；allowed 为生产环境允许测试的路由，给出时只测其中的路由与 GET。"""
    values = {key: config.get(f"sources.api-fuzz.levels.{level.value}.{key}") for key in LEVEL_KEYS}
    method = values["includeMethod"]
    methods = options.include_methods or (() if method is None else (method,))
    if allowed:
        methods = (limits.GET,)
    paths = options.include_paths
    if allowed:
        paths = tuple(path for path in paths if path in allowed) if paths else allowed
    slow = config.tunable("slowResponseSeconds") if plan is None or plan.response_time else None
    return RunParameters(
        max_examples=options.max_examples or values["maxExamples"],
        phases=options.phases or tuple(values["phases"]),
        include_methods=tuple(item.upper() for item in methods),
        include_paths=paths,
        exclude_regex=options.exclude_path_regex
        or spec.exclude_regex(_setting(config, "sources.api-fuzz.exclude", [])),
        max_response_time=options.max_response_time or slow,
        seed=options.seed or seed_source(),
        workers=int(config.get("sources.api-fuzz.workers")),
        sanitize_keys=tuple(_setting(config, "sources.api-fuzz.sanitizeKeys", [])),
        timeout_seconds=SECONDS_PER_MINUTE * float(config.get("sources.api-fuzz.timeoutMinutes")),
        excluded_checks=() if plan is None else plan.excluded,
    )


def executable() -> Path:
    """与当前解释器同一虚拟环境中的 schemathesis 命令。"""
    return Path(sys.executable).parent / SCHEMATHESIS_PACKAGE


def installed_problem(version_of: Callable[[str], str] = metadata.version, command: Path | None = None) -> str | None:
    """Schemathesis 未安装或版本不符时返回提示。"""
    hint = "在 core 目录执行 .venv/bin/pip install -e . 重新安装核心依赖"
    try:
        version = version_of(SCHEMATHESIS_PACKAGE)
    except metadata.PackageNotFoundError:
        return f"Schemathesis 未安装；{hint}"
    if version != SCHEMATHESIS_VERSION:
        return f"Schemathesis 版本为 {version}，锁定版本为 {SCHEMATHESIS_VERSION}；{hint}"
    if not (command or executable()).is_file():
        return f"找不到 schemathesis 命令 {command or executable()}；{hint}"
    return None


def build_argv(params: RunParameters, command: Path, spec_path: Path, config_file: Path, base_url: str,
               role_dir: Path) -> tuple[str, ...]:
    argv = [str(command), "--config-file", str(config_file), "run", str(spec_path), "--url", base_url,
            "--max-examples", str(params.max_examples), "--phases", ",".join(params.phases)]
    for method in params.include_methods:
        argv += ["--include-method", method]
    for path in params.include_paths:
        argv += ["--include-path", path]
    if params.exclude_regex is not None:
        argv += ["--exclude-path-regex", params.exclude_regex]
    if params.excluded_checks:
        argv += ["--exclude-checks", ",".join(params.excluded_checks)]
    if params.max_response_time is not None:
        argv += ["--max-response-time", f"{params.max_response_time:g}"]
    argv += ["--report", "junit,vcr,ndjson", "--report-dir", str(role_dir),
             "--report-ndjson-path", str(role_dir / EVENTS_FILE), "--report-vcr-path", str(role_dir / CASSETTE_FILE),
             "--report-junit-path", str(role_dir / JUNIT_FILE), "--seed", str(params.seed)]
    return tuple(argv)


def role_env(environ: Mapping[str, str], token: str, role: str, model_path: Path | None) -> dict[str, str]:
    """匿名身份的 token 为空，不设置 TIGHTREIN_TOKEN。"""
    extra = {TOKEN_ENV: token} if token else {}
    if model_path is not None:
        extra.update({MODEL_ENV: str(model_path), ROLE_ENV: role})
    return tool_env(environ, extra)


@dataclass(frozen=True)
class RoleRun:
    role: str
    directory: Path
    run: ToolRun

    @property
    def completed(self) -> bool:
        return not self.run.timed_out and self.run.exit_code in NORMAL_EXIT_CODES

    def problem(self) -> str | None:
        if self.completed:
            return None
        if self.run.exit_code == 2:
            return f"角色 {self.role}：Schemathesis 退出码 2(配置或接口描述错误)，见 {LOG_FILE}"
        return f"角色 {self.role}：Schemathesis {self.run.describe()}"


def run_role(launcher: Launcher, params: RunParameters, role: str, token: str, spec_path: Path, config_file: Path,
             base_url: str, role_dir: Path, model_path: Path | None, environ: Mapping[str, str],
             command: Path | None = None) -> RoleRun:
    role_dir.mkdir(parents=True, exist_ok=True)
    argv = build_argv(params, command or executable(), spec_path, config_file, base_url, role_dir)
    run = launcher(ToolCommand(argv, role_dir, params.timeout_seconds, role_env(environ, token, role, model_path),
                               role_dir / LOG_FILE))
    return RoleRun(role, role_dir, run)
