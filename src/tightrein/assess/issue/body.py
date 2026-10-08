"""Issue 正文：直接用取证时一并写好的报告(结论、问题、复现、原因、范围、验收标准)，只按模板排版。

- 小节固定顺序：结论 → 问题 → 影响 → 复现 → 原因 → 范围 → 注意事项 → 验收标准 → 修复方向 → 引用 → 历史；
  没有内容的小节不写(不再写「—」)，每个实施角色少读一些；
- 不再加三条固定的通用验收项(复现测试先败后过、现有测试全部通过、其余行为不变)：实施的通用交付检查已有。只留
  这个 Issue 真正的要求：取证给的验收标准，其后是按来源能由采集复查的条件；
- 观察类来源的条件(部署后的观察期内不再出现某指纹的问题)只能在部署后确认，是验收阶段的标准(`post_deploy`)：
  实施的方案不对应它、审查不判断它，由发布的验收按关联问题的出现确认(44 号计划 6「验收：只做实施阶段做不到的」)；
- 取证给出的「明确不做」写进范围，「必须保持不变」写进注意事项；三类需用户定夺的标记(根因在设计、要动数据结构、
  改公共契约)带理由与位置单独标出；
- 代码位置一律写成反引号中的 `路径:行号`，GitHub 镜像据此换成取证 commit 的永久链接；
- 小节标题与固定文字按项目语言(zh、en、ja，带地区的取主语言，没有的取英文)；读取时任一语言的标题都认；
  拆小节按 CommonMark 规则闭合代码块(同种围栏、长度不短于开头)，代码块里的 `#` 不会切断小节。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SECTIONS = ("conclusion", "problem", "impact", "reproduce", "cause", "scope", "notes", "acceptance", "direction",
            "references", "history")
HEADINGS: dict[str, dict[str, str]] = {
    "conclusion": {"zh": "结论", "en": "Conclusion", "ja": "結論"},
    "problem": {"zh": "问题", "en": "Problem", "ja": "問題"},
    "impact": {"zh": "影响", "en": "Impact", "ja": "影響"},
    "reproduce": {"zh": "复现", "en": "Reproduce", "ja": "再現"},
    "cause": {"zh": "原因", "en": "Cause", "ja": "原因"},
    "scope": {"zh": "范围", "en": "Scope", "ja": "範囲"},
    "notes": {"zh": "注意事项", "en": "Notes", "ja": "注意事項"},
    "acceptance": {"zh": "验收标准", "en": "Acceptance criteria", "ja": "受け入れ基準"},
    "direction": {"zh": "修复方向", "en": "Fix direction", "ja": "修正の方向"},
    "references": {"zh": "引用", "en": "References", "ja": "参照"},
    "history": {"zh": "历史", "en": "History", "ja": "履歴"},
}
TEXTS: dict[str, dict[str, str]] = {
    "expected": {"zh": "期望", "en": "Expected", "ja": "期待"},
    "actual": {"zh": "实际", "en": "Actual", "ja": "実際"},
    "severity": {"zh": "严重度", "en": "Severity", "ja": "重大度"},
    "consequence": {"zh": "后果", "en": "Consequence", "ja": "影響"},
    "roles": {"zh": "受影响的角色", "en": "Affected roles", "ja": "影響を受けるロール"},
    "data": {"zh": "受影响的数据", "en": "Affected data", "ja": "影響を受けるデータ"},
    "callSites": {"zh": "调用点", "en": "Call sites", "ja": "呼び出し箇所"},
    "rootCause": {"zh": "根因位置", "en": "Root cause", "ja": "根本原因の箇所"},
    "trigger": {"zh": "触发条件", "en": "Trigger", "ja": "発生条件"},
    "entries": {"zh": "调用链入口", "en": "Entry points", "ja": "呼び出し元の入口"},
    "introduced": {"zh": "引入", "en": "Introduced by", "ja": "混入コミット"},
    "files": {"zh": "可能涉及的文件", "en": "Files likely involved", "ja": "関係しそうなファイル"},
    "outOfScope": {"zh": "不在本次范围内", "en": "Out of scope", "ja": "今回の対象外"},
    "mustKeep": {"zh": "保持不变", "en": "Must not change", "ja": "変更しない"},
    "discuss": {"zh": "需要先与代码作者讨论(预估改动命中受保护路径)",
                "en": "Discuss with the code author first (touches protected paths)",
                "ja": "先にコード作成者と相談(保護対象のパスに触れる)"},
    "decide": {"zh": "需用户定夺", "en": "Needs a decision", "ja": "判断が必要"},
    "flag.design": {"zh": "根因在设计本身", "en": "Root cause is in the design", "ja": "原因が設計にある"},
    "flag.dataStructure": {"zh": "要动数据结构或存量数据", "en": "Changes data structures or existing data",
                           "ja": "データ構造または既存データの変更が必要"},
    "flag.publicContract": {"zh": "会改变公共实现或接口契约", "en": "Changes a shared implementation or API contract",
                            "ja": "共通実装または API 契約が変わる"},
    "evidenceCommit": {"zh": "取证 commit", "en": "Evidence commit", "ja": "調査時のコミット"},
    "accept.apiFuzz": {"zh": "API 模糊测试对 `{location}` 的 `{check}` 检查以 `{role}` 身份通过",
                       "en": "API fuzzing check `{check}` on `{location}` passes as `{role}`",
                       "ja": "API ファジングの `{check}` チェックが `{location}` で `{role}` として通る"},
    "accept.anonymous": {"zh": "未登录", "en": "anonymous", "ja": "未ログイン"},
    "accept.observed": {"zh": "部署后的观察期与之后的覆盖运行中不再出现指纹为 `{fingerprint}` 的问题",
                        "en": "No problem with fingerprint `{fingerprint}` appears in the post-deploy observation "
                              "window and later covering runs",
                        "ja": "デプロイ後の観察期間とその後の実行でフィンガープリント `{fingerprint}` の問題が出ない"},
    "accept.static": {"zh": "静态巡检的 `{check}` 在 `{location}` 不再命中",
                      "en": "Static check `{check}` no longer matches at `{location}`",
                      "ja": "静的チェック `{check}` が `{location}` で検出されない"},
    "accept.refuted": {"zh": "对原发现重新取证，判定为不成立", "en": "Re-examining the original finding refutes it",
                       "ja": "元の検出を再調査すると不成立と判定される"},
    "history.line": {"zh": "{at} {event}(操作者 {actor})", "en": "{at} {event} (by {actor})",
                     "ja": "{at} {event}(操作者 {actor})"},
}
FLAG_KEYS = ("design", "dataStructure", "publicContract")
DISCUSS = "discuss_with_author"
ELLIPSIS = "…"
OBSERVED_SOURCES = frozenset({"collect.platform_errors", "collect.access_log", "collect.alerts",
                              "collect.project_probes"})
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6}) +(.+?)\s*#*\s*$")
_ANY_HEADING = re.compile(r"^#{1,6} +(.+?)\s*$", re.MULTILINE)
_ALIASES = {title: key for key, titles in HEADINGS.items() for title in titles.values()}
_CHECKBOX = re.compile(r"^\[[ xX]\]\s*")
POST_DEPLOY_KEYS = ("accept.observed",)  # 只能在部署后确认的验收条件(各语言的原文都认)
_POST_DEPLOY = [re.compile("^" + re.escape(template).replace(re.escape("{fingerprint}"), "(?P<fingerprint>.+)") + "$")
                for key in POST_DEPLOY_KEYS for template in TEXTS[key].values()]


@dataclass(frozen=True)
class IssueFacts:
    """排版正文要用的东西(由 create.py 从评估结论整理)。"""

    issue: str
    title: str
    severity: str | None
    output: Mapping[str, Any]  # 取证输出(位置已补全)
    claim: Mapping[str, Any]
    commit: str | None
    introduced: Sequence[Mapping[str, Any]] = ()
    labels: Sequence[str] = ()
    probe_acceptance: Sequence[str] = ()
    problems: Sequence[tuple[str, str]] = field(default_factory=tuple)


def pick(table: Mapping[str, str], language: str) -> str:
    primary = language.split("-")[0].split("_")[0].lower()
    return table.get(primary) or table["en"]


def text(key: str, language: str, **values: Any) -> str:
    found = pick(TEXTS[key], language)
    return found.format(**values) if values else found


def heading(key: str, language: str) -> str:
    return pick(HEADINGS[key], language)


def title_of(output: Mapping[str, Any], claim: Mapping[str, Any], max_length: int) -> str:
    """Issue 标题：report.title，没有时为主张的标题；超长时截断加省略号。"""
    found = str((output.get("report") or {}).get("title") or claim["title"]).strip()
    return found if len(found) <= max_length else found[:max_length - 1].rstrip() + ELLIPSIS


def render(facts: IssueFacts, language: str) -> str:
    """评估来的 Issue 正文；没有内容的小节不写。历史由 files.write 追加。"""
    output = facts.output
    report = output.get("report") or {}
    consequence = (output.get("impact") or {}).get("consequence")
    conclusion = facts.title + (f"；{text('consequence', language)}：{consequence}" if consequence else "")
    content = {
        "conclusion": conclusion,
        "problem": _problem(output, facts.claim, language),
        "impact": _impact(facts, language),
        "reproduce": _numbered(list(report.get("steps") or []) or ([output["trigger"]] if output.get("trigger") else [])),
        "cause": _cause(facts, language),
        "scope": _scope(output, language),
        "notes": _notes(facts, language),
        "acceptance": checkboxes([*(report.get("acceptance") or []), *facts.probe_acceptance]),
        "direction": str((output.get("assessment") or {}).get("direction") or ""),
        "references": _references(facts, language),
    }
    return document(f"{facts.issue} {facts.title}", content, language)


def render_manual(issue: str, title: str, description: str, language: str) -> str:
    """用户自己提的需求：「问题」为用户原文(小标题降为加粗，不打乱小节结构)，原文中验收标准一节提出来。"""
    problem, criteria = requirement_parts(description)
    return document(f"{issue} {title}", {"problem": problem, "acceptance": checkboxes(criteria)}, language)


def document(title: str, content: Mapping[str, str], language: str) -> str:
    parts = [f"# {title}"]
    for key in SECTIONS:
        value = (content.get(key) or "").strip()
        if value:
            parts += [f"## {heading(key, language)}", value]
    return "\n\n".join(parts) + "\n"


def requirement_parts(description: str) -> tuple[str, list[str]]:
    """用户原文拆成「问题」正文与其中验收标准一节的条目(以「- 」开头的行，去掉复选框)。"""
    kept: list[str] = []
    criteria: list[str] = []
    in_acceptance = False
    for line in description.strip().splitlines():
        found = _HEADING.match(line)
        if found is not None:
            in_acceptance = _ALIASES.get(found.group(2).strip()) == "acceptance"
            if in_acceptance:
                continue
        if not in_acceptance:
            kept.append(line)
        elif line.startswith(("- ", "* ")):
            item = _CHECKBOX.sub("", line[2:].strip())
            if item:
                criteria.append(item)
    body = _ANY_HEADING.sub(lambda match: f"**{match.group(1)}**", "\n".join(kept).strip())
    return body, criteria


def split(body: str) -> dict[str, str]:
    """各二级小节的「键(认不出的保留标题) → 内容」；代码块里的 # 行不算标题。"""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence: str | None = None
    for line in body.splitlines():
        marker = _FENCE.match(line)
        if marker is not None:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None and line.startswith("## "):
            title = line[3:].strip()
            current = found.setdefault(_ALIASES.get(title, title), [])
            continue
        if current is not None:
            current.append(line)
    return {key: "\n".join(lines).strip() for key, lines in found.items()}


def acceptance_items(body: str) -> list[str]:
    return [_CHECKBOX.sub("", line[2:].strip()) for line in split(body).get("acceptance", "").splitlines()
            if line.startswith("- ")]


def post_deploy(item: str) -> bool:
    """这条验收标准是不是只能在部署后确认(观察类来源的「观察期内不再出现」)：属于验收阶段，不在实施中对应与判断。"""
    return post_deploy_fingerprint(item) is not None


def post_deploy_fingerprint(item: str) -> str | None:
    """部署后才能确认的验收标准所指的问题指纹；不是这类标准时为 None。"""
    text_ = _CHECKBOX.sub("", item.strip())
    found = next((match for pattern in _POST_DEPLOY if (match := pattern.match(text_)) is not None), None)
    return found.group("fingerprint") if found is not None else None


def post_deploy_items(body: str) -> list[str]:
    """正文「验收标准」一节中只能在部署后确认的条目(发布的验收按它确认)。"""
    return [item for item in acceptance_items(body) if post_deploy(item)]


def checkboxes(items: Sequence[str]) -> str:
    return "\n".join(f"- [ ] {item}" for item in dict.fromkeys(item for item in items if item))


def append_history(body: str, entries: Sequence[Mapping[str, Any]], language: str) -> str:
    """历史是最后一节：在末尾追加；用户删掉了这一节时重新加上标题。"""
    lines = [history_line(entry, language) for entry in entries]
    text_ = body.rstrip()
    if "history" not in split(text_):
        text_ += f"\n\n## {heading('history', language)}\n"
    return text_.rstrip() + "\n" + "\n".join(lines) + "\n"


def history_line(entry: Mapping[str, Any], language: str) -> str:
    line = "- " + text("history.line", language, at=entry["at"], event=entry["event"], actor=entry["actor"])
    detail = "；".join(str(item) for item in (entry.get("reason"), entry.get("note")) if item)
    return line + (f"：{detail}" if detail else "")


def probe_acceptance(source: str, location: str | None, check_type: str, fingerprint: str, role: str | None,
                     language: str) -> list[str]:
    """按来源生成能由采集复查的验收条件。"""
    where = location or "?"
    if source == "collect.api_fuzz":
        return [text("accept.apiFuzz", language, location=where, check=check_type,
                     role=role or text("accept.anonymous", language))]
    if source in OBSERVED_SOURCES:
        return [text("accept.observed", language, fingerprint=fingerprint)]
    if source == "collect.static":
        return [text("accept.static", language, check=check_type, location=where)]
    return [text("accept.refuted", language)]


def _lines(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items if item)


def _numbered(items: Sequence[str]) -> str:
    return "\n".join(f"{number}. {item}" for number, item in enumerate(items, start=1))


def _code(location: str) -> str:
    return f"`{location}`"


def _problem(output: Mapping[str, Any], claim: Mapping[str, Any], language: str) -> str:
    report = output.get("report") or {}
    found = str(report.get("summary") or claim["statement"])
    pairs = [(key, report.get(key)) for key in ("expected", "actual") if report.get(key)]
    if pairs:
        found += "\n\n" + _lines([f"{text(key, language)}：{value}" for key, value in pairs])
    return found


def _impact(facts: IssueFacts, language: str) -> str:
    report = facts.output.get("report") or {}
    items = []
    if facts.severity:
        reason = report.get("severityReason")
        items.append(f"{text('severity', language)}：{facts.severity}" + (f"({reason})" if reason else ""))
    impact = facts.output.get("impact") or {}
    if impact.get("roles"):
        items.append(f"{text('roles', language)}：{'、'.join(impact['roles'])}")
    if impact.get("data"):
        items.append(f"{text('data', language)}：{impact['data']}")
    if impact.get("callSites"):
        items.append(f"{text('callSites', language)}：{'、'.join(_code(site) for site in impact['callSites'])}")
    return _lines(items)


def _cause(facts: IssueFacts, language: str) -> str:
    output = facts.output
    causes = [(_code(str(cause["file"]) + ":" + str(cause["line"])) + " " + str(cause.get("symbol") or "")).rstrip()
              for cause in output.get("rootCauses") or []]
    items = [f"{text('rootCause', language)}：{'、'.join(causes)}" if causes else ""]
    if output.get("trigger"):
        items.append(f"{text('trigger', language)}：{output['trigger']}")
    entries = list(dict.fromkeys(item["entry"] for item in output.get("counterEvidence") or []))
    if entries:
        items.append(f"{text('entries', language)}：{'、'.join(_code(entry) for entry in entries)}")
    introduced = [f"{item['commit'][:12]}" + (f"(PR #{item['pr']})" if item.get("pr") else "")
                  for item in facts.introduced]
    if introduced:
        items.append(f"{text('introduced', language)}：{'、'.join(introduced)}")
    return _lines(items)


def _scope(output: Mapping[str, Any], language: str) -> str:
    assessment = output.get("assessment") or {}
    files = [item["path"] for item in assessment.get("files") or []] or list(
        dict.fromkeys(cause["file"] for cause in output.get("rootCauses") or []))
    items = [f"{text('files', language)}：{'、'.join(_code(file) for file in files)}" if files else ""]
    excluded = list(assessment.get("outOfScope") or [])
    if excluded:
        items.append(f"{text('outOfScope', language)}：{'；'.join(excluded)}")
    return _lines(items)


def _notes(facts: IssueFacts, language: str) -> str:
    assessment = facts.output.get("assessment") or {}
    items = [f"**{text('discuss', language)}**"] if DISCUSS in facts.labels else []
    items += [f"{text('mustKeep', language)}：{item}" for item in assessment.get("mustKeep") or []]
    for key in FLAG_KEYS:
        flag = (assessment.get("flags") or {}).get(key) or {}
        if flag.get("flagged"):
            where = "、".join(_code(location) for location in flag.get("locations") or [])
            items.append(f"{text('decide', language)}：{text('flag.' + key, language)}：{flag.get('reason')}"
                         + (f"({where})" if where else ""))
    return _lines(items)


def _references(facts: IssueFacts, language: str) -> str:
    items = [f"{text('evidenceCommit', language)}：{facts.commit}" if facts.commit else ""]
    items += [f"{_code(fact['location'])} {fact['observation']}" for fact in facts.output.get("facts") or []]
    items += [f"`{problem_id}` {title}".rstrip() for problem_id, title in facts.problems]
    return _lines(items)
