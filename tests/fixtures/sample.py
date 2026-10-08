"""整体测试与重新录制共用的样例：示例项目仓库、示例工作区、假 GitHub、录制的模型输出(回放与录制)。

- 示例项目 demo-app：calc.py 的 average([]) 除以零崩溃(缺陷)，tests/ 下有 pytest 测试；origin 为本地裸仓库，
  远程地址写成 GitHub 的形式(GitHub 的 owner/name 由它推出)，再用 git 的 insteadOf 改写到本地裸仓库：
  fetch、push 都落在本地，不联网；
- 示例工作区 demo：只启用不需联网的来源——项目探针(scripts/probe_average.py 真跑 average([]) 检查)与任务外发现；
  另启用验收，其余模块不启用；overrides.json 写进 settings.json 的 overrides(登记探针；偶发信号出现 1 次即成问题，
  省一轮运行)；关卡(Issue 放行、定案、合并)用缺省的自动规则；
- 子进程(Gateway)：git、pytest、探针真跑；gh 交给假 GitHub；联网程序与 agent 工具(claude、agy、codex)一律拒绝；
- 假 GitHub(FakeGitHub)：gh 命令的替身，经 ProcessRunner 应答 PR 的建、查、检查与合并；合并时在裸仓库上真的合进
  main(git merge-tree + commit-tree，不需要工作目录)；
- 回放(SampleReplay)：录制集 recordings/ 按 agents/tools/replay.py 的格式；提示里的样例根目录、解释器路径与 ULID 编号
  换成占位符再算哈希，录制在任何机器、任何临时目录下都能回放；
- 录制(Recorder)：answers/ 下按调用点手写的结构化结果(与可选的 <调用点>.patch)，跑一遍整体流程，把每次调用写成
  claude stream-json 格式的 stdout.jsonl 与 index.json(见 tests/README.md「重新录制」)；
- 情形(SCENARIOS)：fix 为一次修好(answers/ → recordings/)；regression 为第一次修复合并后探针仍报 → 回归、撤销 PR、
  退回待修 → 第二次修复 → 验收通过(answers/regression/ 优先、其余取 answers/ → recordings_regression/)。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tightrein.agents.call import default_adapters
from tightrein.agents.params import CallParams, Model
from tightrein.agents.tools import Resume
from tightrein.agents.tools.replay import INDEX, PATCH, STDOUT, ReplayAdapter, index_entry
from tightrein.cli import main as cli_main
from tightrein.cli.assemble import Externals
from tightrein.onboard.setup import MODULES
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.process import Command, Outcome, ProcessRunner, SubprocessRunner
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

FIXTURES = Path(__file__).resolve().parent
REPO_ROOT = FIXTURES.parents[1]
DEFAULTS = REPO_ROOT / "settings" / "defaults.json"
RECORDINGS = FIXTURES / "recordings"
ANSWERS = FIXTURES / "answers"
FIX = "fix"
REGRESSION = "regression"
PROBE_SCRIPT = FIXTURES / "probe_average.py"

PROJECT = "demo"
SLUG = "demo/demo-app"
REMOTE_URL = f"https://github.com/{SLUG}.git"
START = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)
HOST = "sample-host"
STEP = timedelta(hours=13)  # 两次运行之间拨快的时间：合并后第二次运行在观察期内，第三次越过观察期(合并 + 1h + 24h)
MAX_RUNS = 8
# 占位符：录制与回放时提示里的这些路径换成固定文字，哈希不随机器与临时目录变化
ROOT_MARK = "<sample>"
PYTHON_MARK = "<python>"
# 信号等记录的编号是 ULID(时间加随机数)，每次运行都不同
ULID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")
ULID_MARK = "<ulid>"

CALC = '''"""示例项目：几个统计函数。"""


def total(values):
    return sum(values)


def average(values):
    return total(values) / len(values)
'''
TESTS = '''from calc import average, total


def test_total():
    assert total([1, 2, 3]) == 6


def test_average():
    assert average([1, 2, 3]) == 2
'''
PYPROJECT = '''[project]
name = "demo-app"
version = "0.1.0"

[tool.pytest.ini_options]
pythonpath = ["."]
'''
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "Demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
                "GIT_COMMITTER_NAME": "Demo", "GIT_COMMITTER_EMAIL": "demo@example.com"}
FIXED_DATE = "2026-10-01T00:00:00Z"


@dataclass(frozen=True)
class Scenario:
    """整体测试的一种情形：手写的模型结果(按顺序找，前面的优先)与录制集。"""

    name: str
    answers: tuple[Path, ...]
    recordings: Path


SCENARIOS = {
    FIX: Scenario(FIX, (ANSWERS,), RECORDINGS),
    REGRESSION: Scenario(REGRESSION, (ANSWERS / REGRESSION, ANSWERS), FIXTURES / "recordings_regression"),
}


@dataclass(frozen=True)
class Sample:
    root: Path
    repo: Path
    origin: Path
    tool: ToolLayout
    layout: WorkspaceLayout
    home: Path


# 示例项目与工作区


def build(root: Path) -> Sample:
    """在 root 下建示例项目(带本地裸远程)与工具目录、示例工作区。"""
    root.mkdir(parents=True, exist_ok=True)
    repo, origin, home = root / "demo-app", root / "demo-app-origin.git", root / "home"
    home.mkdir(exist_ok=True)
    _repo(repo, origin)
    tool = ToolLayout(root / "tool")
    tool.settings_dir.mkdir(parents=True)
    shutil.copy(DEFAULTS, tool.defaults)
    layout = _workspace(tool, repo)
    return Sample(root=root, repo=repo, origin=origin, tool=tool, layout=layout, home=home)


def git(cwd: Path, *args: str) -> str:
    """样例自己的 git 操作(建仓库、假 GitHub 合并)。固定作者与日期：同样的内容得到同样的 commit，
    录制的提示里出现的 commit 不随每次建仓库变化。"""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC", "GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE,
           **GIT_IDENTITY}
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
    return done.stdout


def _repo(repo: Path, origin: Path) -> None:
    git(repo.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    for path, text in {"calc.py": CALC, "tests/test_calc.py": TESTS, "pyproject.toml": PYPROJECT,
                       ".gitignore": "__pycache__/\n.pytest_cache/\n"}.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for key, value in (("user.name", "Demo"), ("user.email", "demo@example.com"),
                       ("remote.origin.url", REMOTE_URL), ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
                       (f"url.{origin}.insteadOf", REMOTE_URL)):
        git(repo, "config", key, value)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: init")
    git(repo, "push", "-q", "-u", "origin", "main")


def _workspace(tool: ToolLayout, repo: Path) -> WorkspaceLayout:
    layout = tool.workspace(PROJECT)
    layout.data_dir.mkdir(parents=True)
    layout.scripts_dir.mkdir()
    shutil.copy(PROBE_SCRIPT, layout.scripts_dir / PROBE_SCRIPT.name)
    enabled = {"collect.project_probes", "collect.incidental", "release.accept"}
    modules = {key: {"status": "enabled", "method": None, "script": None, "guide": None, "reason": None,
                     "impact": None} if key in enabled else
               {"status": "disabled", "method": None, "script": None, "guide": None, "reason": "示例项目不需要",
                "impact": "整体测试不覆盖"} for key in MODULES}
    _write(layout.setup, {"project": PROJECT, "updatedAt": "2026-10-08T00:00:00Z", "modules": modules})
    facts = {"repo": str(repo), "mainBranch": "main", "language": "zh",
             "commands": {"test": f"{sys.executable} -m pytest -q -p no:cacheprovider", "lint": None, "build": None,
                          "typecheck": None},
             "testPatterns": ["tests/**", "test_*.py"], "frontendPatterns": None}
    overrides = json.loads((FIXTURES / "overrides.json").read_text(encoding="utf-8"))
    _write(layout.settings, {"project": facts, "overrides": overrides})
    _write(layout.sites, {})
    _write(layout.secrets, {})
    layout.secrets.chmod(0o600)
    return layout


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# 子进程：真跑 git、pytest 与探针，gh 由假 GitHub 应答，联网的命令直接判失败


NETWORK_PROGRAMS = frozenset({"curl", "wget", "ssh", "scp", "nc", "claude", "agy", "codex", "semgrep", "npx"})


class Gateway:
    """测试的 ProcessRunner：记下每条命令；gh 交给假 GitHub；联网或调用真实模型的程序拒绝并记进 refused。"""

    def __init__(self, github: FakeGitHub) -> None:
        self.github = github
        self.real = SubprocessRunner()
        self.commands: list[tuple[str, ...]] = []
        self.refused: list[tuple[str, ...]] = []

    def run(self, command: Command) -> Outcome:
        self.commands.append(command.argv)
        program = Path(command.argv[0]).name
        if program == "gh":
            return self.github.run(command)
        if program in NETWORK_PROGRAMS:
            self.refused.append(command.argv)
            return Outcome(127, "", f"整体测试不允许 {program}", 0, None, None)
        return self.real.run(command)


@dataclass
class PullRecord:
    number: int
    head_ref: str
    base: str
    title: str
    body: str
    state: str = "OPEN"
    merged_at: str | None = None
    merge_commit: str | None = None
    comments: list[str] = field(default_factory=list)


class FakeGitHub:
    """gh 的替身：PR 存在内存里，合并在裸仓库上真的做；检查(CI)一律通过。不认识的命令退出码 1 并记进 unknown。"""

    def __init__(self, origin: Path, clock: Callable[[], datetime]) -> None:
        self.origin = origin
        self.clock = clock
        self.pulls: dict[int, PullRecord] = {}
        self.calls: list[tuple[str, ...]] = []
        self.unknown: list[tuple[str, ...]] = []

    def run(self, command: Command) -> Outcome:
        args = _strip_repo(command.argv[1:])
        self.calls.append(args)
        stdin = command.stdin.decode() if isinstance(command.stdin, bytes) else command.stdin or ""
        try:
            answer = self._answer(args, stdin)
        except LookupError as error:
            return Outcome(1, "", str(error), 0, None, None)
        if answer is None:
            self.unknown.append(args)
            return Outcome(1, "", f"假 GitHub 不认识：gh {' '.join(args)}", 0, None, None)
        return Outcome(0, answer, "", 0, None, None)

    def _answer(self, args: tuple[str, ...], stdin: str) -> str | None:
        head = args[:2]
        if head == ("pr", "list"):
            return json.dumps(self._list(args))
        if head == ("pr", "view"):
            return json.dumps(self._view(self._pull(args[2]), _option(args, "--json")))
        if head == ("pr", "create"):
            number = len(self.pulls) + 1
            self.pulls[number] = PullRecord(number, _option(args, "--head"), _option(args, "--base"),
                                            _option(args, "--title"), stdin)
            return f"https://github.com/{SLUG}/pull/{number}\n"
        if head == ("pr", "edit"):
            self._pull(args[2]).body = stdin
            return ""
        if head == ("pr", "checks"):
            return json.dumps([{"name": "test", "state": "SUCCESS", "bucket": "pass"}])
        if head == ("pr", "comment"):
            pull = self._pull(args[2])
            pull.comments.append(stdin)
            return f"https://github.com/{SLUG}/pull/{pull.number}#issuecomment-{len(pull.comments)}\n"
        if head == ("pr", "merge"):
            self._merge(self._pull(args[2]))
            return ""
        if head == ("pr", "review") or head == ("pr", "ready"):
            return ""
        if args[:1] == ("api",):
            return self._api(args[1])
        if head == ("repo", "view"):
            return json.dumps({"isPrivate": True, "hasIssuesEnabled": True})
        return None

    def _list(self, args: tuple[str, ...]) -> list[dict[str, Any]]:
        fields = _option(args, "--json").split(",")
        pulls = list(self.pulls.values())
        if "--head" in args:
            pulls = [pull for pull in pulls if pull.head_ref == _option(args, "--head")]
        if "--search" in args:
            commit = _option(args, "--search")
            pulls = [pull for pull in pulls if pull.merge_commit == commit or self._contains(pull, commit)]
        state = _option(args, "--state") if "--state" in args else "open"
        if state != "all":
            pulls = [pull for pull in pulls if pull.state.lower() == state]
        return [self._view(pull, ",".join(fields)) for pull in pulls]

    def _view(self, pull: PullRecord, fields: str) -> dict[str, Any]:
        head = self._ref(pull.head_ref)
        values: dict[str, Any] = {
            "number": pull.number, "url": f"https://github.com/{SLUG}/pull/{pull.number}", "state": pull.state,
            "headRefName": pull.head_ref, "headRefOid": head, "mergedAt": pull.merged_at,
            "mergeCommit": {"oid": pull.merge_commit} if pull.merge_commit else None, "closedAt": pull.merged_at,
            "reviewDecision": "", "body": pull.body, "isDraft": False,
            "mergeable": "MERGEABLE" if pull.state == "OPEN" else "UNKNOWN",
            "mergeStateStatus": "CLEAN" if pull.state == "OPEN" else "UNKNOWN", "reviews": [],
            "comments": [{"body": body, "url": f"https://github.com/{SLUG}/pull/{pull.number}#c{index}"}
                         for index, body in enumerate(pull.comments)],
            "title": pull.title, "baseRefName": pull.base, "statusCheckRollup": [],
        }
        return {name: values.get(name) for name in fields.split(",") if name}

    def _api(self, path: str) -> str | None:
        if "/rules/branches/" in path:
            return "[]"
        if "/branches/" in path:
            return json.dumps({"name": path.rsplit("/", 1)[-1], "protected": False})
        return None

    def _merge(self, pull: PullRecord) -> None:
        """在裸仓库上合并(squash)：main 前进一个提交，删除远程分支。"""
        base = self._ref(pull.base)
        tree = git(self.origin, "merge-tree", "--write-tree", base, pull.head_ref).split()[0]
        message = f"{pull.title} (#{pull.number})"
        commit = git(self.origin, "commit-tree", tree, "-p", base, "-m", message).strip()
        git(self.origin, "update-ref", f"refs/heads/{pull.base}", commit)
        git(self.origin, "update-ref", "-d", f"refs/heads/{pull.head_ref}")
        pull.state, pull.merge_commit = "MERGED", commit
        pull.merged_at = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _ref(self, ref: str) -> str | None:
        done = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{ref}"], cwd=self.origin,
                              capture_output=True, text=True, check=False)
        return done.stdout.strip() or None

    def _contains(self, pull: PullRecord, commit: str) -> bool:
        return pull.merge_commit is not None and pull.merge_commit.startswith(commit)

    def _pull(self, number: str) -> PullRecord:
        found = self.pulls.get(int(number))
        if found is None:
            raise LookupError(f"没有 PR {number}")
        return found


def _strip_repo(args: tuple[str, ...]) -> tuple[str, ...]:
    if "--repo" not in args:
        return args
    index = args.index("--repo")
    return args[:index] + args[index + 2:]


def _option(args: tuple[str, ...], name: str) -> str:
    return args[args.index(name) + 1]


# 录制的模型输出：回放与录制


class SampleReplay(ReplayAdapter):
    """提示里的样例根目录与解释器路径换成占位符再交给回放：哈希只随提示的实质内容变化。"""

    def __init__(self, root: Path, runner: ProcessRunner, sample: Path) -> None:
        super().__init__(root, default_adapters(), runner)
        self.marks = {str(sample.resolve()): ROOT_MARK, str(sample): ROOT_MARK, sys.executable: PYTHON_MARK}
        self.calls: list[tuple[str, str | None, int | None]] = []

    def portable(self, params: CallParams) -> CallParams:
        prompt = params.prompt
        for actual, mark in sorted(self.marks.items(), key=lambda item: -len(item[0])):
            prompt = prompt.replace(actual, mark)
        prompt = ULID.sub(ULID_MARK, prompt)
        return replace(params, prompt=prompt)

    def build(self, params: CallParams, model: Model, *, executable: str, env: dict[str, str],
              schema: dict[str, Any] | None, scratch: Path, resume: Resume | None) -> Command:
        self.calls.append((params.point, params.subject, params.round))
        return super().build(self.portable(params), model, executable=executable, env=env, schema=schema,
                             scratch=scratch, resume=resume)


class Recorder(SampleReplay):
    """重新录制：按 answers 下的 <调用点>[.r<轮>][.c<第几次调用>].json 给出结构化结果，写成录制后照常回放。

    同一(调用点、对象、轮次)的第几次调用：重试、续接，或同一 Issue 回归后又一次修复时的同一步；带 .c<次> 的只给那一次，
    不带的给每一次。改动(.patch)只打在第 1 次调用上，之后的调用要用带 .c<次> 的文件另给。"""

    def __init__(self, root: Path, runner: ProcessRunner, sample: Path, answers: tuple[Path, ...]) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / INDEX).write_text(json.dumps({"recordings": []}), encoding="utf-8")
        super().__init__(root, runner, sample)
        self.answers = answers
        self.entries: list[dict[str, Any]] = []
        self.missing: list[str] = []

    def build(self, params: CallParams, model: Model, *, executable: str, env: dict[str, str],
              schema: dict[str, Any] | None, scratch: Path, resume: Resume | None) -> Command:
        portable = self.portable(params)
        number = self._calls[(params.point, params.subject, params.round)] + 1
        answer = self._answer(params, number)
        if answer is None:
            self.missing.append(f"{params.point}(对象 {params.subject}，轮 {params.round})")
        else:
            directory = f"{len(self.entries) + 1:02d}-{params.point}" + (f"-r{params.round}" if params.round else "") \
                + (f"-c{number}" if number > 1 else "")
            target = self.root / directory
            target.mkdir(parents=True, exist_ok=True)
            (target / STDOUT).write_text(stream_json(answer, f"session-{len(self.entries) + 1}"), encoding="utf-8")
            patch = self._file(params, ".patch", number)
            if patch is not None:
                shutil.copy(patch, target / PATCH)
            self.entries.append(index_entry(portable, number, directory, "claude"))
            (self.root / INDEX).write_text(json.dumps({"recordings": self.entries}, ensure_ascii=False, indent=2)
                                           + "\n", encoding="utf-8")
            self._entries[(params.point, params.subject, params.round, number)] = self.entries[-1]
        return super().build(params, model, executable=executable, env=env, schema=schema, scratch=scratch,
                             resume=resume)

    def _answer(self, params: CallParams, number: int) -> dict[str, Any] | None:
        path = self._file(params, ".json", number)
        return None if path is None else json.loads(path.read_text(encoding="utf-8"))

    def _file(self, params: CallParams, suffix: str, number: int) -> Path | None:
        rounds = [f".r{params.round}"] if params.round else []
        names = [f"{params.point}{part}.c{number}{suffix}" for part in [*rounds, ""]]
        if suffix != ".patch" or number == 1:
            names += [f"{params.point}{part}{suffix}" for part in [*rounds, ""]]
        found = (directory / name for name in names for directory in self.answers)
        return next((path for path in found if path.is_file()), None)


def stream_json(answer: dict[str, Any], session: str) -> str:
    """claude -p --output-format stream-json 的最小输出：会话、一条带用量的回复、结果(带结构化输出)。"""
    usage = {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    text = json.dumps(answer, ensure_ascii=False)
    lines = [
        {"type": "system", "subtype": "init", "session_id": session},
        {"type": "assistant", "message": {"id": f"msg-{session}", "content": [{"type": "text", "text": text}],
                                          "usage": usage}},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": session, "result": text,
         "structured_output": answer, "usage": usage, "total_cost_usd": 0.0, "num_turns": 1},
    ]
    return "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n"


# 运行


@dataclass
class Harness:
    sample: Sample
    clock: FixedClock
    github: FakeGitHub
    runner: Gateway
    replay: SampleReplay
    externals: Externals

    def __call__(self, *argv: str) -> SimpleNamespace:
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main([*argv, "-p", PROJECT, "--json"], self.externals, stdin=io.StringIO(""), stdout=out,
                             stderr=err)
        text = out.getvalue()
        data = json.loads(text.splitlines()[-1]) if text.strip() else {}
        return SimpleNamespace(code=code, data=data, out=text, err=err.getvalue())


def harness(root: Path, *, record: bool = False, scenario: str = FIX) -> Harness:
    sample = build(root)
    clock = FixedClock(START)
    github = FakeGitHub(sample.origin, clock.now)
    runner = Gateway(github)
    found = SCENARIOS[scenario]
    replay = Recorder(root / "recordings", runner, root, found.answers) if record \
        else SampleReplay(found.recordings, runner, root)
    environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(sample.home), "USER": "demo",
               "LANG": "C.UTF-8", "TZ": "UTC"}
    externals = Externals(
        environ=environ, runner=runner, clock=clock, tool=sample.tool, home=sample.home, cwd=sample.root,
        program=Path(sys.executable).parent / "tightrein", pid=os.getpid(), host=HOST, uid=os.getuid(),
        stdin_is_tty=lambda: False, alive=lambda pid: pid == os.getpid(), terminate=lambda pid: None,
        replay=replay, platform="linux",
    )
    cli_main.build_parser.cache_clear()
    return Harness(sample, clock, github, runner, replay, externals)


@contextmanager
def offline() -> Iterator[list[Any]]:
    """本进程内不许连网络：连接 AF_INET/AF_INET6 的地址即失败，记下尝试的地址。"""
    attempts: list[Any] = []
    original = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(address)
            raise OSError(f"整体测试不允许联网：{address}")
        return original(self, address)

    socket.socket.connect = guarded  # type: ignore[method-assign]
    try:
        yield attempts
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]


def issue_statuses(sample: Sample) -> dict[str, str]:
    conn = sqlite3.connect(sample.layout.database)
    try:
        return dict(conn.execute("SELECT id, status FROM issues ORDER BY id").fetchall())
    finally:
        conn.close()


def drive(h: Harness, *, runs: int = MAX_RUNS) -> list[SimpleNamespace]:
    """按调度推进：每次 `tightrein run` 后把时钟拨快 STEP，直到所有 Issue 都结束(done 或 cancelled)或跑满 runs 次。"""
    results = []
    for _ in range(runs):
        results.append(h("run"))
        statuses = issue_statuses(h.sample)
        if statuses and all(status in ("done", "cancelled") for status in statuses.values()):
            break
        h.clock.advance(STEP)
    return results
