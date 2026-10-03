"""评测用例的哈希与封存(architecture/03 2.6.1，design 14.6)。

- 每个模块用例目录按相对路径排序，逐个文件把「相对路径 + NUL + 文件内容」送入 SHA-256 得到该用例的哈希；
  evals/retrieval/cases.jsonl 按整个文件计算；
- evals/manifest.json 记录全部用例的哈希：`{"schemaVersion": 1, "cases": {"<模块>/<用例编号>": "<sha256>",
  "retrieval/cases.jsonl": "<sha256>"}}`；与磁盘逐项比较，不一致、多出与缺少的全部列出；
- 用例的任何变更都必须以提交的形式留在 git 历史中：经 vcs 执行只读的 `git status --porcelain -- evals`，
  有输出即为未提交的改动；`git rev-parse HEAD:./evals` 给出评测所用用例集的树对象编号；
- seal 只在交互终端中、且不是 agent 执行器启动的进程(没有 TIGHTREIN_RUN_ID)时执行：列出差异，用户确认后
  重算并写入 manifest.json，写一个 user_action 事件。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from tightrein.domain.enums import Stage
from tightrein.evaluation.errors import CaseProblem, SealRefused
from tightrein.observability.tracing import ENV_RUN_ID, Tracer
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs.process import VcsProcess

MANIFEST_VERSION = 1
CASE_PATTERN = "E-*"
CHANGED = "内容与封存时不同"
UNREGISTERED = "没有登记在 manifest.json 中"
MISSING = "manifest.json 中登记了但目录或文件不存在"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def case_key(layout: WorkspaceLayout, path: Path) -> str:
    return path.relative_to(layout.evals_dir()).as_posix()


def module_case_dirs(layout: WorkspaceLayout) -> list[Path]:
    found = []
    for stage in Stage:
        directory = layout.evals_dir() / stage.value
        if directory.is_dir():
            found += [path for path in sorted(directory.glob(CASE_PATTERN)) if path.is_dir()]
    return found


def compute(layout: WorkspaceLayout) -> dict[str, str]:
    hashes = {case_key(layout, path): case_sha256(path) for path in module_case_dirs(layout)}
    cases = layout.retrieval_cases()
    if cases.is_file():
        hashes[case_key(layout, cases)] = file_sha256(cases)
    return dict(sorted(hashes.items()))


def read(layout: WorkspaceLayout) -> dict[str, str]:
    path = layout.eval_manifest()
    if not path.is_file():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8"))["cases"])


def manifest_sha256(layout: WorkspaceLayout) -> str:
    path = layout.eval_manifest()
    return file_sha256(path) if path.is_file() else hashlib.sha256(b"").hexdigest()


def differences(expected: Mapping[str, str], actual: Mapping[str, str]) -> list[CaseProblem]:
    problems = []
    for key in sorted(set(expected) | set(actual)):
        if key not in actual:
            problems.append(CaseProblem(key, MISSING))
        elif key not in expected:
            problems.append(CaseProblem(key, UNREGISTERED))
        elif expected[key] != actual[key]:
            problems.append(CaseProblem(key, CHANGED))
    return problems


def uncommitted(process: VcsProcess, layout: WorkspaceLayout) -> list[CaseProblem]:
    output = process.git(layout.root, "status", "--porcelain", "--untracked-files=all", "--",
                         layout.relative(layout.evals_dir())).stdout
    return [CaseProblem(line[3:], f"未提交的改动({line[:2].strip()})") for line in output.splitlines() if line.strip()]


def evals_tree(process: VcsProcess, layout: WorkspaceLayout) -> str:
    return process.git(layout.root, "rev-parse", f"HEAD:./{layout.relative(layout.evals_dir())}").stdout.strip()


def seal(layout: WorkspaceLayout, *, interactive: bool, environ: Mapping[str, str],
         confirm: Callable[[Sequence[CaseProblem]], bool], tracer: Tracer) -> str:
    """用户确认后重算 manifest.json，返回它的 sha256；没有差异时不写文件。"""
    if not interactive:
        raise SealRefused("eval seal 只能在交互终端中由用户执行")
    if environ.get(ENV_RUN_ID):
        raise SealRefused("eval seal 不能在 agent 执行器启动的进程中执行")
    actual = compute(layout)
    changes = differences(read(layout), actual)
    if not changes:
        return manifest_sha256(layout)
    if not confirm(changes):
        raise SealRefused("用户没有确认，manifest.json 保持不变")
    text = json.dumps({"schemaVersion": MANIFEST_VERSION, "cases": actual}, ensure_ascii=False, indent=2) + "\n"
    atomic.write_text(layout.eval_manifest(), text)
    tracer.event("user_action", decision="eval-seal", reason=f"重新封存 {len(changes)} 处变化",
                 artifact=layout.relative(layout.eval_manifest()),
                 attributes={"changes": [str(problem) for problem in changes]})
    return manifest_sha256(layout)
