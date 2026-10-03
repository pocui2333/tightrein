"""评测用例的加载与校验(architecture/03 2.3、2.6.1、1.9)。

每次评测(以及检索评测)开始前依次检查，任何一项不通过都不运行任何用例：
1. manifest：全部用例的哈希与 evals/manifest.json 逐项一致，没有多出或缺少的用例；
2. 未提交改动：evals/ 在 git 中没有未提交的改动；
3. schema：每个 case.json 与 cases.jsonl 的每一行符合 data/eval-case.schema.json；用例编号与目录名一致、模块与所在目录
   一致、input.handoff 在 input/ 下存在、expected.excludeItems 中的评分项在评分表中存在(评分表由调用方给出)；
4. 记录 manifest 文件自身的哈希与 evals/ 的树对象编号，写进 plan.json 与报告。
fix 另加运行即评测的用例(run_cases，data/eval/cases/)：由程序在修复部署后确认通过时保存，不经 manifest 封存，
不合格的文件列为 EvalCaseInvalid。前两项为 EvalCaseTampered，第三项为 EvalCaseInvalid。add_case 由用户在终端中执行，建立下一个编号的用例目录。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.contracts import validate
from tightrein.domain import ids
from tightrein.domain.enums import EvalCaseCategory, ScoreResult, Stage
from tightrein.evaluation import manifest, run_cases
from tightrein.evaluation.errors import CaseProblem, EvalCaseInvalid, EvalCaseTampered, SealRefused
from tightrein.observability.tracing import ENV_RUN_ID
from tightrein.retrieval.errors import InvalidQuery
from tightrein.retrieval.models import DEFAULT_LIMIT, SearchFilters
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs.process import VcsProcess

SCHEMA = "data/eval-case.schema.json"
CASE_FILE = "case.json"
INPUT_DIR = "input"
GATES_FILE = "gates.json"
REPLAY_DIR = "replay"
RUN_SOURCE = "run"
CASE_VERSION = 1

RubricItems = Callable[[Stage], set[str]]


@dataclass(frozen=True)
class Assertion:
    item_id: str
    path: str
    op: str
    value: Any
    description: str
    has_value: bool


@dataclass(frozen=True)
class ModuleCase:
    id: str
    module: Stage
    title: str
    category: EvalCaseCategory
    source: Mapping[str, Any]
    handoff: str
    commit: str
    args: tuple[str, ...]
    exclude_items: Mapping[str, str]
    assertions: tuple[Assertion, ...]
    replay_expectation: Mapping[str, ScoreResult]
    directory: Path
    input_path: Path | None = None

    @property
    def key(self) -> str:
        return f"{self.module.value}/{self.id}"

    @property
    def input_file(self) -> Path:
        """封存的用例为 <用例>/input/<交接文档>；运行即评测的用例(run_cases)直接给出 input_path。"""
        return self.input_path or self.directory / INPUT_DIR / self.handoff

    @property
    def gates_file(self) -> Path | None:
        path = self.directory / GATES_FILE
        return path if path.is_file() else None

    @property
    def replay_dir(self) -> Path | None:
        path = self.directory / REPLAY_DIR
        return path if path.is_dir() else None


@dataclass(frozen=True)
class RetrievalCase:
    id: str
    query: str
    filters: SearchFilters
    expected: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class VerifiedCases:
    """校验通过的用例集。"""

    module_cases: list[ModuleCase]
    retrieval_cases: list[RetrievalCase]
    case_hashes: dict[str, str]
    manifest_sha256: str
    evals_tree: str


def _module_case(directory: Path, data: Mapping[str, Any]) -> ModuleCase:
    expected = data.get("expected", {})
    assertions = tuple(
        Assertion(f"assert-{number}", item["path"], item["op"], item.get("value"), item["description"],
                  "value" in item)
        for number, item in enumerate(expected.get("assertions", ()), start=1)
    )
    return ModuleCase(
        id=data["id"], module=Stage(data["module"]), title=data["title"], category=EvalCaseCategory(data["category"]),
        source=data["source"], handoff=data["input"]["handoff"], commit=data["input"]["commit"],
        args=tuple(data["input"].get("args", ())),
        exclude_items={item["itemId"]: item["reason"] for item in expected.get("excludeItems", ())},
        assertions=assertions,
        replay_expectation={item["itemId"]: ScoreResult(item["result"]) for item in data.get("replayExpectation", ())},
        directory=directory,
    )


def run_case(layout: WorkspaceLayout, data: Mapping[str, Any]) -> ModuleCase:
    """运行即评测的用例(run_cases)：编号与来源对象为 Issue 编号，以修复前的提交为基准。"""
    directory = layout.run_cases_dir()
    return ModuleCase(
        id=data["issueId"], module=Stage.FIX, title=data["title"], category=EvalCaseCategory.REPRESENTATIVE,
        source={"kind": RUN_SOURCE, "subjectId": data["issueId"], "mergeCommit": data.get("mergeCommit")},
        handoff=data["input"], commit=data["baseCommit"], args=(), exclude_items={}, assertions=(),
        replay_expectation={}, directory=directory, input_path=directory / data["input"])


def load_module_case(layout: WorkspaceLayout, directory: Path,
                     rubric_items: RubricItems | None = None) -> tuple[ModuleCase | None, list[CaseProblem]]:
    key = manifest.case_key(layout, directory)
    path = directory / CASE_FILE
    if not path.is_file():
        return None, [CaseProblem(key, f"缺少 {CASE_FILE}")]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return None, [CaseProblem(key, f"{CASE_FILE} 不是合法的 JSON：{error}")]
    errors = validate.validate(SCHEMA, data)
    if errors or data.get("kind") != "module":
        reasons = [f"{error.path}: {error.reason}" for error in errors] or ["kind 须为 module"]
        return None, [CaseProblem(key, reason) for reason in reasons]
    case = _module_case(directory, data)
    problems = []
    if case.id != directory.name:
        problems.append(CaseProblem(key, f"用例编号 {case.id} 与目录名 {directory.name} 不一致"))
    if case.module.value != directory.parent.name:
        problems.append(CaseProblem(key, f"module 为 {case.module.value}，却位于 {directory.parent.name} 目录"))
    if not case.input_file.is_file():
        problems.append(CaseProblem(key, f"输入文件 {INPUT_DIR}/{case.handoff} 不存在"))
    if rubric_items is not None:
        known = rubric_items(case.module)
        problems += [CaseProblem(key, f"excludeItems 中的评分项 {item} 不在 {case.module.value} 的评分表中")
                     for item in case.exclude_items if item not in known]
    return (None if problems else case), problems


def load_retrieval_cases(layout: WorkspaceLayout) -> tuple[list[RetrievalCase], list[CaseProblem]]:
    path = layout.retrieval_cases()
    key = manifest.case_key(layout, path)
    if not path.is_file():
        return [], []
    cases, problems, seen = [], [], set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as error:
            problems.append(CaseProblem(key, f"第 {number} 行不是合法的 JSON：{error}"))
            continue
        errors = validate.validate(SCHEMA, data)
        if errors or data.get("kind") != "retrieval":
            reasons = [f"{error.path}: {error.reason}" for error in errors] or ["kind 须为 retrieval"]
            problems += [CaseProblem(key, f"第 {number} 行 {reason}") for reason in reasons]
            continue
        if data["id"] in seen:
            problems.append(CaseProblem(key, f"第 {number} 行的用例编号 {data['id']} 重复"))
            continue
        seen.add(data["id"])
        filters = data["filters"]
        try:
            parsed = SearchFilters.parse(tuple(filters.get("types", ())), tuple(filters.get("tags", ())),
                                         filters.get("status", "active"), filters.get("limit", DEFAULT_LIMIT))
        except InvalidQuery as error:
            problems.append(CaseProblem(key, f"第 {number} 行的过滤条件不合法：{error}"))
            continue
        cases.append(RetrievalCase(data["id"], data["query"], parsed, tuple(data["expected"]), data["source"]))
    return cases, problems


def verify_cases(layout: WorkspaceLayout, process: VcsProcess, module: Stage | None = None,
                 rubric_items: RubricItems | None = None) -> VerifiedCases:
    """按 2.6.1 校验；module 给出时只加载该模块的用例，哈希与未提交改动仍检查整个 evals/。fix 另加运行即评测的
    用例(data/eval/cases/，由程序保存，不经 manifest 封存)，其哈希一并记入 case_hashes。"""
    actual = manifest.compute(layout)
    tampered = manifest.differences(manifest.read(layout), actual) + manifest.uncommitted(process, layout)
    if tampered:
        raise EvalCaseTampered(tampered)
    module_cases, problems = [], []
    for directory in manifest.module_case_dirs(layout):
        if module is not None and directory.parent.name != module.value:
            continue
        case, found = load_module_case(layout, directory, rubric_items)
        problems += found
        if case is not None:
            module_cases.append(case)
    hashes = dict(actual)
    if module in (None, Stage.FIX):
        recorded, run_hashes, found = run_cases.read(layout)
        problems += found
        module_cases += [run_case(layout, data) for data in recorded]
        hashes.update(run_hashes)
    retrieval_cases, found = load_retrieval_cases(layout)
    problems += found
    if problems:
        raise EvalCaseInvalid(problems)
    return VerifiedCases(module_cases, retrieval_cases, hashes, manifest.manifest_sha256(layout),
                         manifest.evals_tree(process, layout))


def add_case(layout: WorkspaceLayout, module: Stage, handoff: Path, commit: str, *, interactive: bool,
             environ: Mapping[str, str]) -> Path:
    """建立下一个编号的用例目录，复制输入交接文档，生成供用户填写的 case.json 骨架；不封存。"""
    if not interactive:
        raise SealRefused("eval add 只能在交互终端中由用户执行")
    if environ.get(ENV_RUN_ID):
        raise SealRefused("eval add 不能在 agent 执行器启动的进程中执行")
    existing = [ids.parse_sequence(path.name) for path in manifest.module_case_dirs(layout)
                if path.parent.name == module.value]
    case_id = ids.eval_case_id(max(existing, default=0) + 1)
    directory = layout.eval_case_dir(module, case_id)
    (directory / INPUT_DIR).mkdir(parents=True)
    shutil.copyfile(handoff, directory / INPUT_DIR / handoff.name)
    source = json.loads(handoff.read_text(encoding="utf-8"))
    skeleton = {
        "schemaVersion": CASE_VERSION, "id": case_id, "kind": "module", "module": module.value, "title": "",
        "category": EvalCaseCategory.REPRESENTATIVE.value,
        "source": {"runId": source.get("runId", ""), "subjectId": source.get("subject", {}).get("id", ""),
                   "correction": None},
        "input": {"handoff": handoff.name, "commit": commit},
        "expected": {"excludeItems": [], "assertions": []},
    }
    atomic.write_text(directory / CASE_FILE, json.dumps(skeleton, ensure_ascii=False, indent=2) + "\n")
    return directory

