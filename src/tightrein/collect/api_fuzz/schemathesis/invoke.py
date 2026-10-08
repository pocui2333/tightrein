"""调用 Schemathesis CLI(锁定版本 4.28.0)。

- `--config-file` 是 Schemathesis 4 的顶层选项，必须写在 `run` 之前，写在后面会被当成 run 的未知参数报错；
- 只查服务端报错：`--checks not_a_server_error`；
- 报告用 `--report junit,vcr,ndjson` 并分别指定路径；只解析 NDJSON，其余两种留给人看；
- `--seed` 每次随机生成并记进信号，复现时可以复用；
- 凭证只经环境变量 TIGHTREIN_TOKEN 传入(匿名运行不设)，不进命令行；
- 命令输出先脱敏(本次的 token、调用方的 Redactor 登记的值与凭据格式)再写进 schemathesis.log，原文不落盘；
- 退出码 0 与 1 都算正常完成(1 表示有失败用例)；2 是配置或接口描述错误；超时与其他退出码都算这次调用失败，
  失败的调用不产出信号也不计入覆盖；
- 用与当前解释器同一虚拟环境中的命令；未安装或版本不符时直接失败并给出重装提示，不用错误版本跑。
"""

from __future__ import annotations

import random
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from tightrein.collect.api_fuzz.limits import Restriction
from tightrein.collect.api_fuzz.schemathesis.config_writer import TOKEN_ENV
from tightrein.protocol.naming import parse_duration
from tightrein.protocol.process import Command, Outcome, ProcessRunner
from tightrein.protocol.security import Redactor, child_env
from tightrein.settings.load import Settings
from tightrein.store.files.atomic import write_text

SCHEMATHESIS_VERSION = "4.28.0"
SCHEMATHESIS_PACKAGE = "schemathesis"
SERVER_ERROR_CHECK = "not_a_server_error"
EVENTS_FILE = "events.ndjson"
CASSETTE_FILE = "cassette.yaml"
JUNIT_FILE = "junit.xml"
LOG_FILE = "schemathesis.log"
NORMAL_EXIT_CODES = (0, 1)
CONFIG_ERROR_EXIT = 2
SEED_LIMIT = 2**31 - 1
REINSTALL_HINT = "在 tightrein 仓库执行 .venv/bin/pip install -e . 重新安装依赖"

SOURCE = "collect.api_fuzz"

SeedSource = Callable[[], int]


@dataclass(frozen=True)
class RunParameters:
    max_examples: int
    phases: tuple[str, ...]
    include_methods: tuple[str, ...]  # 为空时不限方法
    include_paths: tuple[str, ...]  # 为空时不限路由
    exclude_regex: str | None
    seed: int
    workers: int
    sanitize_keys: tuple[str, ...]
    timeout_s: float


@dataclass(frozen=True)
class Invocation:
    directory: Path
    outcome: Outcome

    @property
    def completed(self) -> bool:
        return self.outcome.stopped_by is None and self.outcome.exit_code in NORMAL_EXIT_CODES

    def problem(self) -> str | None:
        if self.completed:
            return None
        if self.outcome.start_error is not None:
            return f"Schemathesis 无法启动：{self.outcome.start_error}"
        if self.outcome.exit_code == CONFIG_ERROR_EXIT:
            return f"Schemathesis 退出码 2(配置或接口描述错误)，见 {LOG_FILE}"
        if self.outcome.stopped_by is not None:
            return f"Schemathesis 被终止({self.outcome.stopped_by})"
        return f"Schemathesis 退出码 {self.outcome.exit_code}：{self.outcome.stderr_tail[-500:]}"


def parameters(settings: Settings, restriction: Restriction, seed_source: SeedSource) -> RunParameters:
    """取值在 controls."collect.api_fuzz"；生产环境的方法与路由由 restriction 限定(limits.check 的结果)。"""
    section = settings.section(SOURCE)
    patterns = [f"(?:{pattern})" for pattern in section["exclude"]]
    return RunParameters(
        max_examples=int(section["maxExamples"]),
        phases=tuple(section["phases"]),
        include_methods=restriction.methods,
        include_paths=restriction.paths,
        exclude_regex="|".join(patterns) or None,
        seed=seed_source(),
        workers=int(section["workers"]),
        sanitize_keys=tuple(section["sanitizeKeys"]),
        timeout_s=parse_duration(section["fuzzTimeout"]),
    )


def random_seed() -> int:
    return random.SystemRandom().randint(1, SEED_LIMIT)


def executable() -> Path:
    return Path(sys.executable).parent / SCHEMATHESIS_PACKAGE


def installed_problem(version_of: Callable[[str], str] = metadata.version, command: Path | None = None) -> str | None:
    """未安装或版本不符时返回提示。"""
    try:
        version = version_of(SCHEMATHESIS_PACKAGE)
    except metadata.PackageNotFoundError:
        return f"Schemathesis 未安装；{REINSTALL_HINT}"
    if version != SCHEMATHESIS_VERSION:
        return f"Schemathesis 版本为 {version}，锁定版本为 {SCHEMATHESIS_VERSION}；{REINSTALL_HINT}"
    path = command or executable()
    if not path.is_file():
        return f"找不到 schemathesis 命令 {path}；{REINSTALL_HINT}"
    return None


def build_argv(params: RunParameters, command: Path, spec_path: Path, config_file: Path, base_url: str,
               report_dir: Path) -> tuple[str, ...]:
    argv = [str(command), "--config-file", str(config_file), "run", str(spec_path), "--url", base_url,
            "--checks", SERVER_ERROR_CHECK, "--max-examples", str(params.max_examples),
            "--phases", ",".join(params.phases)]
    for method in params.include_methods:
        argv += ["--include-method", method]
    for path in params.include_paths:
        argv += ["--include-path", path]
    if params.exclude_regex is not None:
        argv += ["--exclude-path-regex", params.exclude_regex]
    argv += ["--report", "junit,vcr,ndjson", "--report-dir", str(report_dir),
             "--report-ndjson-path", str(report_dir / EVENTS_FILE),
             "--report-vcr-path", str(report_dir / CASSETTE_FILE),
             "--report-junit-path", str(report_dir / JUNIT_FILE), "--seed", str(params.seed)]
    return tuple(argv)


def environment(environ: Mapping[str, str], token: str) -> dict[str, str]:
    """匿名运行的 token 为空，不设 TIGHTREIN_TOKEN。"""
    return child_env(environ, set_values={TOKEN_ENV: token} if token else {})


def run(runner: ProcessRunner, params: RunParameters, *, token: str, spec_path: Path, config_file: Path,
        base_url: str, report_dir: Path, environ: Mapping[str, str], command: Path | None = None,
        redactor: Redactor | None = None) -> Invocation:
    report_dir.mkdir(parents=True, exist_ok=True)
    argv = build_argv(params, command or executable(), spec_path, config_file, base_url, report_dir)
    outcome = runner.run(Command(argv, report_dir, environment(environ, token), timeout_s=params.timeout_s))
    write_text(report_dir / LOG_FILE, redact_log(outcome.stdout, token, redactor))
    return Invocation(report_dir, outcome)


def redact_log(text: str, token: str, redactor: Redactor | None) -> str:
    """日志落盘前脱敏：先换掉本次的 token(调用方的 Redactor 未必登记过它)，再按调用方的 Redactor 处理。"""
    local = Redactor()
    local.register(token)
    return (redactor or Redactor()).text(local.text(text))
