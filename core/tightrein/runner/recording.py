"""回放录制集的读取与校验(architecture/02 2.12)。

录制集目录：
- index.json：(role, subjectId, attempt) 到录制目录的映射与录制时任务的哈希，按 runner/replay-index.schema.json 校验；
- <角色>-<对象编号>.<attempt>/result.json：执行器结果(路径字段不使用)；transcript.jsonl：统一格式的会话记录；
  changes.patch(可选)：workspace-write 任务在 worktree 中产生的改动；first-call.json(可选)：第一次调用的结构化结果，
  用于构造「第一次不合格、重试成功」的情形；stdout.jsonl(可选)：工具的原始输出，只用于适配器测试。
录制就是真实运行留下的 raw/runner/ 与 transcripts/ 中的文件，复制到录制集目录后补上 index.json 即可。
任务哈希覆盖提示(任务说明、skill 与上下文)、schema 与访问级别；与录制时不一致时不回放。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.contracts import validate
from tightrein.runner.task import RunnerTask

INDEX = "index.json"
RESULT = "result.json"
TRANSCRIPT = "transcript.jsonl"
PATCH = "changes.patch"
FIRST_CALL = "first-call.json"
INDEX_SCHEMA = "runner/replay-index.schema.json"
RESULT_SCHEMA = "runner/runner-result.schema.json"
PATH_FIELDS = ("transcriptPath", "guardReport")


class RecordingError(Exception):
    """录制集不合格：index.json 或 result.json 不符合 schema、录制目录不存在。"""


def task_sha256(task: RunnerTask) -> str:
    data = {"instructions": task.instructions.to_dict(), "outputSchema": task.output_schema,
            "access": task.access.value}
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def index_entry(task: RunnerTask, directory: str) -> dict[str, Any]:
    return {"role": task.role, "subjectId": task.subject_id, "attempt": task.attempt, "dir": directory,
            "taskSha256": task_sha256(task)}


@dataclass(frozen=True)
class Recording:
    directory: Path
    task_sha256: str
    result: dict[str, Any]
    events: list[dict[str, Any]]
    patch: Path | None
    first_call: Any
    has_first_call: bool


class RecordingSet:
    def __init__(self, root: Path) -> None:
        self.root = root
        index_path = root / INDEX
        if not index_path.is_file():
            raise RecordingError(f"录制集中没有 {INDEX}：{root}")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        errors = validate.validate(INDEX_SCHEMA, index)
        if errors:
            raise RecordingError(f"{index_path} 不合格：{'; '.join(str(error) for error in errors)}")
        self._entries = {(item["role"], item["subjectId"], item["attempt"]): item for item in index["recordings"]}

    def find(self, role: str, subject_id: str, attempt: int) -> Recording | None:
        entry = self._entries.get((role, subject_id, attempt))
        if entry is None:
            return None
        directory = self.root / entry["dir"]
        result_path = directory / RESULT
        if not result_path.is_file():
            raise RecordingError(f"录制目录中没有 {RESULT}：{directory}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        errors = validate.validate(RESULT_SCHEMA, {**result, **{name: None for name in PATH_FIELDS}})
        if errors:
            raise RecordingError(f"{result_path} 不合格：{'; '.join(str(error) for error in errors)}")
        transcript = directory / TRANSCRIPT
        events = ([json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
                  if transcript.is_file() else [])
        patch = directory / PATCH
        first = directory / FIRST_CALL
        return Recording(directory, entry["taskSha256"], result, events, patch if patch.is_file() else None,
                         json.loads(first.read_text(encoding="utf-8")) if first.is_file() else None, first.is_file())
