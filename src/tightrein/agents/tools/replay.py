"""回放：不启动工具、不调用模型，按录制的工具原始输出走与真实调用相同的路径(逐行兜底、解析、schema 校验、边界、
用量累计)。整体测试(tests/whole/)用它。

录制集目录：
- index.json：`{"recordings": [{"point", "subject", "round", "call", "dir", "tool", "exitCode", "sha256"}]}`；
  call 为同一(调用点、对象、轮次)的第几次工具调用(含重试与续接)，sha256 见 call_sha256；
- <dir>/stdout.jsonl：录制时那个工具的原始输出(调试模式保存的 raw 去掉 tightrein 分隔行即是)；
- <dir>/changes.patch(可选)：可写调用在工作目录中产生的改动，回放带它的那次调用时用 `git apply` 打上，边界检查照常
  生效；录制时只给产生改动的那次调用放(同一轮的重试、续接不带；同一 Issue 回归后又一次修复的编码带它自己的)。
提示、schema 或访问级别与录制时不同(哈希不符)即拒绝回放，结果为失败，要求重新录制。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.agents.params import CallParams, Model
from tightrein.agents.result import CallStatus
from tightrein.agents.tools import NOTHING, Adapter, CallConfigError, LineEvent, Parsed, Resume
from tightrein.protocol.process import Command, Outcome, ProcessRunner

NAME = "replay"
INDEX = "index.json"
STDOUT = "stdout.jsonl"
PATCH = "changes.patch"
ENTRY_KEYS = ("point", "subject", "round", "call", "dir", "tool", "exitCode", "sha256")
MISSING = "replay-missing"


@dataclass(frozen=True)
class Recording:
    directory: Path
    tool: str
    exit_code: int
    sha256: str
    patch: Path | None


def call_sha256(params: CallParams) -> str:
    """覆盖提示、schema 与访问级别：这三样变了，录制的输出就不再对应这次调用。"""
    data = {"prompt": params.prompt, "schema": params.schema, "access": params.access.value}
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def index_entry(params: CallParams, call: int, directory: str, tool: str, exit_code: int = 0) -> dict[str, Any]:
    return {"point": params.point, "subject": params.subject, "round": params.round, "call": call, "dir": directory,
            "tool": tool, "exitCode": exit_code, "sha256": call_sha256(params)}


class ReplayAdapter:
    """同时充当适配器与 ProcessRunner：build 取录制，run 把录制的输出逐行交给 on_line，parse 交给录制时的工具。"""

    name = NAME
    native_turns = False
    checks_commands = True  # 录制的命令当时已经过检查
    env_names: tuple[str, ...] = ()

    def __init__(self, root: Path, adapters: Mapping[str, Adapter], runner: ProcessRunner) -> None:
        self.root = root
        self.adapters = dict(adapters)
        self.runner = runner
        self._entries = _index(root)
        self._calls: Counter[tuple[str, str | None, int | None]] = Counter()
        self._lock = threading.Lock()
        self._current = threading.local()  # 本线程正在回放的录制，parse_line、parse 按它找录制时的工具

    def env_values(self, params: CallParams) -> dict[str, str]:
        return {}

    def build(self, params: CallParams, model: Model, *, executable: str, env: dict[str, str],
              schema: dict[str, Any] | None, scratch: Path, resume: Resume | None) -> Command:
        key = (params.point, params.subject, params.round)
        with self._lock:
            self._calls[key] += 1
            number = self._calls[key]
        entry = self._entries.get((*key, number))
        self._current.tool = None
        if entry is None:
            reason = f"{MISSING}：录制集中没有 {params.point} {params.subject} 第 {number} 次调用的录制"
        elif entry["sha256"] != call_sha256(params):
            reason = "replay-task-changed：提示、schema 或访问级别与录制时不同，需要重新录制"
        else:
            reason = ""
            self._current.tool = entry["tool"]
        directory = "" if entry is None else entry["dir"]
        exit_code = "1" if entry is None else str(entry["exitCode"])
        return Command(argv=(NAME, directory, str(number), exit_code, reason), cwd=params.workdir, env=env)

    def run(self, command: Command) -> Outcome:
        _, directory, _, exit_code, reason = command.argv
        if reason:
            return Outcome(exit_code=1, stdout="", stderr_tail=reason, duration_ms=0, stopped_by=None, start_error=None)
        recording = self.root / directory
        patch = recording / PATCH
        if patch.is_file():
            applied = self.runner.run(replace(command, argv=("git", "apply", "--whitespace=nowarn", str(patch)),
                                              on_line=None, stdin=None))
            if applied.exit_code != 0:
                return Outcome(exit_code=1, stdout="", stderr_tail=f"录制的改动无法应用到 {command.cwd}："
                               f"{applied.stderr_tail or applied.start_error}", duration_ms=0, stopped_by=None,
                               start_error=None)
        lines = (recording / STDOUT).read_text(encoding="utf-8").splitlines()
        stopped = None
        for index, line in enumerate(lines):
            stopped = command.on_line(line) if command.on_line is not None else None
            if stopped is not None:
                lines = lines[:index + 1]
                break
        return Outcome(exit_code=None if stopped else int(exit_code), stdout="\n".join(lines), stderr_tail="",
                       duration_ms=0, stopped_by=stopped, start_error=None)

    def parse_line(self, line: str) -> LineEvent:
        tool = getattr(self._current, "tool", None)
        return NOTHING if tool is None else self._adapter(tool).parse_line(line)

    def parse(self, stdout: str, stderr: str, exit_code: int | None, now: datetime) -> Parsed:
        tool = getattr(self._current, "tool", None)
        if tool is None:
            return Parsed(CallStatus.FAILED, error=stderr)
        return self._adapter(tool).parse(stdout, stderr, exit_code, now)

    def _adapter(self, tool: str) -> Adapter:
        if tool not in self.adapters:
            raise CallConfigError(f"录制用的工具 {tool} 没有适配器")
        return self.adapters[tool]


def _index(root: Path) -> dict[tuple[str, str | None, int | None, int], dict[str, Any]]:
    path = root / INDEX
    try:
        recordings = json.loads(path.read_text(encoding="utf-8"))["recordings"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CallConfigError(f"录制集不合格：{path}：{error}") from error
    entries = {}
    for entry in recordings:
        missing = [key for key in ENTRY_KEYS if not isinstance(entry, dict) or key not in entry]
        if missing:
            raise CallConfigError(f"录制集 {path} 的条目缺少 {'、'.join(missing)}：{entry}")
        if not (root / entry["dir"] / STDOUT).is_file():
            raise CallConfigError(f"录制目录中没有 {STDOUT}：{root / entry['dir']}")
        entries[(entry["point"], entry["subject"], entry["round"], entry["call"])] = entry
    return entries
