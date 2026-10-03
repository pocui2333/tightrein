"""代码评分器注册表(architecture/03 2.6.4，design 12.2)。评分表中 method 为 code 的项按 scorer 名称取这里的实现。

| 评分器 | 判定 |
|---|---|
| evidence-locations | params.paths 取到的每条证据(`文件:行号[-止]` 或 {file, line})在代码快照中存在且行号不超过文件行数 |
| vague-wording | params.fields 取到的文字不含 params.words 中的任何词 |
| values-in | params.path 取到至少一个值，且全部属于 params.allowed |
| verdict-fields | params.path 的判定属于 params.allowed；params.required 中列出的判定要求对应路径取到非空值 |
| refuted-source | 现象来源的 location 与 factRef 恰好给出一个：位置在代码快照中存在，或序号在 claim.facts 的范围内 |
| counter-check | 至少一条反证检查；入口属于 claim.entryPoints 或是真实存在的位置；上游有校验时带真实存在的位置 |
| diff-size | 沙箱输出的 diff 改动文件数与增删行数不超过 thresholds.fix 的上限 |
| diff-protected | diff 没有触及受保护路径与受保护标记 |
| diff-tests | diff 没有修改测试或复现检查路径，没有新增跳过标记 |
| document-sections | params.document 指向的人读文档含 params.sections 中的全部二级标题；params.keyed 为真时按 Issue 小节键比对(任一语言的标题或旧标题) |
| document-locations | 文档中 params.section 一节至少有一处 `文件:行号`，且都在代码快照中存在；params.keyed 同上 |
| absolute-dates | 文档不含 params.words 中的相对日期说法 |

diff 由 guards 的规则重新计算，不采用交接文档中的自述；缺少判定所需的代码快照或 diff 时为 unknown。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.domain import issue_sections
from tightrein.domain.enums import ScoreResult
from tightrein.evaluation.scorers.assertions import values_at
from tightrein.evaluation.scorers.base import CodeScorer, ScoreOutcome, ScoringContext
from tightrein.guards import diff_rules
from tightrein.guards.diff_rules import ChangedFile
from tightrein.guards.report import Violation
from tightrein.vcs.parse import parse_patch

LOCATION = re.compile(r"(?<![\w/.-])([\w./-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?")
SECTION = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)
NO_SNAPSHOT = "没有被测项目的代码快照，无法核对"
NO_DIFF = "沙箱输出中没有改动记录，无法重新计算"


def _pass(reason: str, evidence: Sequence[str] = ()) -> ScoreOutcome:
    return ScoreOutcome(ScoreResult.PASS, reason, tuple(evidence))


def _fail(reason: str, evidence: Sequence[str] = ()) -> ScoreOutcome:
    return ScoreOutcome(ScoreResult.FAIL, reason, tuple(evidence))


def _unknown(reason: str) -> ScoreOutcome:
    return ScoreOutcome(ScoreResult.UNKNOWN, reason)


def _location(value: Any) -> tuple[str, int, int] | None:
    if isinstance(value, Mapping) and isinstance(value.get("file"), str) and isinstance(value.get("line"), int):
        return value["file"], value["line"], value["line"]
    if isinstance(value, str):
        match = re.fullmatch(r"([^:\s][^:]*):(\d+)(?:-(\d+))?", value)
        if match:
            return match.group(1), int(match.group(2)), int(match.group(3) or match.group(2))
    return None


def _check_location(snapshot: Path, file: str, last: int) -> str | None:
    path = (snapshot / file).resolve()
    if snapshot.resolve() not in path.parents or not path.is_file():
        return f"{file} 在代码快照中不存在"
    lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    if last > lines:
        return f"{file} 只有 {lines} 行，引用了第 {last} 行"
    return None


def evidence_locations(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    if context.project_snapshot is None:
        return _unknown(NO_SNAPSHOT)
    found, problems = [], []
    for path in params["paths"]:
        for where, value in values_at(outputs, path):
            if value is None:
                continue
            location = _location(value)
            if location is None:
                problems.append(f"outputs.{where} 不是「文件路径:行号」：{value!r}")
                continue
            file, first, last = location
            found.append(f"{file}:{first}" if first == last else f"{file}:{first}-{last}")
            problem = _check_location(context.project_snapshot, file, last)
            if problem is not None:
                problems.append(problem)
    if problems:
        return _fail("；".join(problems), found)
    if not found:
        return _fail("没有带「文件路径:行号」的证据")
    return _pass(f"{len(found)} 处证据的位置都真实存在", found)


def vague_wording(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    hits = []
    for path in params["fields"]:
        for where, value in values_at(outputs, path):
            if isinstance(value, str):
                hits += [f"outputs.{where} 含「{word}」" for word in params["words"] if word in value]
    if hits:
        return _fail("结论含含糊措辞：" + "；".join(hits))
    return _pass("结论没有含糊措辞")


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def verdict_fields(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    found = values_at(outputs, params["path"])
    verdict = found[0][1] if found else None
    if verdict not in params["allowed"]:
        return _fail(f"outputs.{params['path']} 为 {verdict!r}，只能是 {'、'.join(params['allowed'])}")
    required = params.get("required", {}).get(verdict)
    if required is not None and all(_empty(value) for _, value in values_at(outputs, required)):
        return _fail(f"判定为 {verdict} 时 outputs.{required} 不能为空")
    return _pass("判定属于四档之一，条件必填字段齐全")


def location_problem(snapshot: Path, value: Any) -> str | None:
    """value 不是「文件路径:行号」、文件在快照中不存在或行号越界时返回原因；修复的勘察与评审同样用它核对位置。"""
    location = _location(value)
    if location is None:
        return f"{value!r} 不是「文件路径:行号」"
    return _check_location(snapshot, location[0], location[2])


def refuted_source(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    source = (outputs.get("evidence") or {}).get("sourceOfPhenomenon")
    if not source:
        return _fail("判定为不成立，但没有给出现象来源")
    location, fact = source.get("location"), source.get("factRef")
    if (location is None) == (fact is None):
        return _fail("现象来源须在 location 与 factRef 中恰好给出一个")
    if fact is not None:
        facts = (outputs.get("claim") or {}).get("facts") or []
        if not 1 <= fact <= len(facts):
            return _fail(f"factRef {fact} 不是主张中事实的序号(共 {len(facts)} 条)")
        return _pass(f"现象来源为主张的第 {fact} 条事实")
    if context.project_snapshot is None:
        return _unknown(NO_SNAPSHOT)
    problem = location_problem(context.project_snapshot, location)
    if problem is not None:
        return _fail(f"现象来源的位置不合格：{problem}", [str(location)])
    return _pass("现象来源指向真实存在的代码位置", [str(location)])


def counter_check(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    items = (outputs.get("evidence") or {}).get("counterEvidence") or []
    if not items:
        return _fail("没有反证检查，至少追到一个入口")
    entry_points = set((outputs.get("claim") or {}).get("entryPoints") or [])
    to_check: list[tuple[int, str, Any]] = []
    problems: list[str] = []
    for number, item in enumerate(items, start=1):
        entry = item.get("entry")
        if entry not in entry_points:
            to_check.append((number, "入口", entry))
        upstream = item.get("upstreamValidation") or {}
        if upstream.get("status") == "present":
            if upstream.get("location") is None:
                problems.append(f"第 {number} 条写明上游有校验，但没有给出校验的位置")
            else:
                to_check.append((number, "上游校验", upstream["location"]))
    if to_check and context.project_snapshot is None and not problems:
        return _unknown(NO_SNAPSHOT)
    for number, what, value in to_check:
        if context.project_snapshot is None:
            break
        problem = location_problem(context.project_snapshot, value)
        if problem is not None:
            problems.append(f"第 {number} 条的{what}：{problem}")
    if problems:
        return _fail("；".join(problems))
    return _pass(f"{len(items)} 条反证检查都写明了入口与上游校验")


def values_in(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    found = values_at(outputs, params["path"])
    if not found:
        return _fail(f"{params['path']} 没有任何结果")
    wrong = [f"outputs.{where} 为 {value!r}" for where, value in found if value not in params["allowed"]]
    if wrong:
        return _fail("；".join(wrong))
    return _pass(f"{params['path']} 的 {len(found)} 个结果都属于 {params['allowed']}")


def changed_files(diff_text: str) -> list[ChangedFile]:
    return [ChangedFile(patch.path, patch.added, patch.removed,
                        len(patch.added), len(patch.removed)) for patch in parse_patch(diff_text)]


def _violations_outcome(violations: Sequence[Violation], ok: str) -> ScoreOutcome:
    if violations:
        return _fail("；".join(f"{item.path or '整体'}：{item.detail}" for item in violations),
                     [item.path for item in violations if item.path])
    return _pass(ok)


def diff_size(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    if context.diff_text is None:
        return _unknown(NO_DIFF)
    changes = changed_files(context.diff_text)
    return _violations_outcome(diff_rules.size_violations(changes, context.guard_settings),
                               f"改动 {len(changes)} 个文件，在上限之内")


def diff_protected(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    if context.diff_text is None:
        return _unknown(NO_DIFF)
    changes = changed_files(context.diff_text)
    return _violations_outcome(diff_rules.protected_violations(changes, context.guard_settings, ()),
                               "没有触及受保护文件")


def diff_tests(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    if context.diff_text is None:
        return _unknown(NO_DIFF)
    changes = changed_files(context.diff_text)
    violations = diff_rules.modified_test_violations(changes, context.guard_settings)
    violations += diff_rules.skip_violations(changes, context.guard_settings)
    return _violations_outcome(violations, "没有修改测试与复现检查，也没有新增跳过标记")


def diff_in_plan(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    if context.diff_text is None:
        return _unknown(NO_DIFF)
    planned = [value for _, value in values_at(outputs, params["path"])]
    return _violations_outcome(diff_rules.outside_plan_violations(changed_files(context.diff_text), planned),
                               "改动文件都在计划中")


def diff_residue(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    if context.diff_text is None:
        return _unknown(NO_DIFF)
    violations = diff_rules.residue_violations(changed_files(context.diff_text), context.guard_settings)
    return _violations_outcome(violations, "新增行中没有调试残留")


def _document(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> str | ScoreOutcome:
    found = values_at(outputs, params["document"])
    if not found or not isinstance(found[0][1], str):
        return _fail(f"outputs.{params['document']} 没有给出人读文档的路径")
    if context.output_dir is None:
        return _unknown("没有沙箱输出目录，无法读取人读文档")
    path = context.output_dir / found[0][1]
    if not path.is_file():
        return _fail(f"人读文档 {found[0][1]} 不存在")
    return path.read_text(encoding="utf-8")


def _sections(text: str, keyed: bool = False) -> dict[str, str]:
    """各二级标题与内容；keyed 时按 Issue 版式切分(交接文档版式摊平「内容」下的小节)，以小节键代替标题
    (不认识的标题保持原样)。"""
    if keyed:
        found: dict[str, str] = {}
        for title, content in issue_sections.split(text).items():
            name = issue_sections.key_of(title) or title
            found[name] = found.get(name, "") + content
        return found
    matches = list(SECTION.finditer(text))
    plain: dict[str, str] = {}
    for index, match in enumerate(matches):
        content = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        plain[match.group(1)] = plain.get(match.group(1), "") + content
    return plain


def section_location_problems(snapshot: Path, section: str) -> tuple[list[str], list[str]]:
    """一节文字中的 `文件:行号` 及其中不存在或越界的原因；提 Issue 前的检查与 document-locations 共用。"""
    found = [(match.group(1), int(match.group(3) or match.group(2))) for match in LOCATION.finditer(section)]
    problems = [problem for problem in (_check_location(snapshot, file, last) for file, last in found)
                if problem is not None]
    return [match.group(0) for match in LOCATION.finditer(section)], problems


def document_sections(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    text = _document(outputs, params, context)
    if isinstance(text, ScoreOutcome):
        return text
    missing = [name for name in params["sections"] if name not in _sections(text, bool(params.get("keyed")))]
    if missing:
        return _fail(f"缺少章节：{'、'.join(missing)}")
    return _pass("必需章节齐全")


def document_locations(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    text = _document(outputs, params, context)
    if isinstance(text, ScoreOutcome):
        return text
    if context.project_snapshot is None:
        return _unknown(NO_SNAPSHOT)
    section = _sections(text, bool(params.get("keyed"))).get(params["section"])
    if section is None:
        return _fail(f"没有「{params['section']}」一节")
    evidence, problems = section_location_problems(context.project_snapshot, section)
    if not evidence:
        return _fail(f"「{params['section']}」一节没有「文件路径:行号」")
    return _fail("；".join(problems), evidence) if problems else _pass(f"{len(evidence)} 处位置都真实存在", evidence)


def absolute_dates(outputs: Mapping[str, Any], params: Mapping[str, Any], context: ScoringContext) -> ScoreOutcome:
    text = _document(outputs, params, context)
    if isinstance(text, ScoreOutcome):
        return text
    used = [word for word in params["words"] if word in text]
    if used:
        return _fail(f"使用了相对日期：{'、'.join(used)}")
    return _pass("日期都是绝对日期")


REGISTRY: dict[str, CodeScorer] = {
    "evidence-locations": evidence_locations,
    "vague-wording": vague_wording,
    "values-in": values_in,
    "verdict-fields": verdict_fields,
    "refuted-source": refuted_source,
    "counter-check": counter_check,
    "diff-size": diff_size,
    "diff-protected": diff_protected,
    "diff-tests": diff_tests,
    "diff-in-plan": diff_in_plan,
    "diff-residue": diff_residue,
    "document-sections": document_sections,
    "document-locations": document_locations,
    "absolute-dates": absolute_dates,
}
