"""交接文档的类型注册与小节标题，全部类型定义集中在这里。

每份文档的正文由六个基础小节组成(BASE_SECTIONS，二级标题)，「内容」之下是该类型的固定小节(三级标题)。
新增一种类型：在 HEADINGS 中给新的小节键补上 zh、en、ja 三语标题，在 TYPES 中登记类型，再在
contracts/schemas/handoff/types/<类型>.schema.json 的 `$defs` 中定义它的数据块(没有数据块时 `$defs` 为空)。
标题按 project.language 写入；读取时任何一种语言的标题都识别为同一个键。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from tightrein.domain import language

# 基础小节
CONCLUSION = "conclusion"
CONTENT = "content"
DECISIONS = "decisions"
NEXT = "next"
REFERENCES = "references"
HISTORY = "history"
BASE_SECTIONS = (CONCLUSION, CONTENT, DECISIONS, NEXT, REFERENCES, HISTORY)

HEADINGS: dict[str, dict[str, str]] = {
    # 基础小节
    CONCLUSION: {"zh": "结论", "en": "Conclusion", "ja": "結論"},
    CONTENT: {"zh": "内容", "en": "Content", "ja": "内容"},
    DECISIONS: {"zh": "需要决定", "en": "Decisions needed", "ja": "要判断事項"},
    NEXT: {"zh": "下一步", "en": "Next steps", "ja": "次のステップ"},
    REFERENCES: {"zh": "引用", "en": "References", "ja": "参照"},
    HISTORY: {"zh": "历史", "en": "History", "ja": "履歴"},
    # task
    "goal": {"zh": "目标", "en": "Goal", "ja": "目的"},
    "inputs": {"zh": "输入", "en": "Inputs", "ja": "入力"},
    "constraints": {"zh": "约束", "en": "Constraints", "ja": "制約"},
    "acceptance": {"zh": "验收标准", "en": "Acceptance criteria", "ja": "受け入れ基準"},
    "deliverables": {"zh": "产出要求", "en": "Deliverables", "ja": "成果物の要件"},
    # result
    "done": {"zh": "做了什么", "en": "What was done", "ja": "実施内容"},
    "outputs": {"zh": "产出", "en": "Outputs", "ja": "成果物"},
    "evidence": {"zh": "证据", "en": "Evidence", "ja": "証拠"},
    "deviations": {"zh": "偏离与原因", "en": "Deviations and reasons", "ja": "計画との差異と理由"},
    # finding
    "symptom": {"zh": "现象", "en": "Symptom", "ja": "現象"},
    "location": {"zh": "位置", "en": "Location", "ja": "箇所"},
    "severityHint": {"zh": "严重度提示", "en": "Severity hint", "ja": "重大度の目安"},
    # decision
    "background": {"zh": "背景", "en": "Background", "ja": "背景"},
    "options": {"zh": "选项及利弊", "en": "Options and trade-offs", "ja": "選択肢と得失"},
    "recommendation": {"zh": "推荐与理由", "en": "Recommendation and reasons", "ja": "推奨案と理由"},
    # plan
    "approach": {"zh": "方案", "en": "Approach", "ja": "方針"},
    "steps": {"zh": "文件与步骤", "en": "Files and steps", "ja": "ファイルと手順"},
    "acceptanceMapping": {"zh": "验收对照", "en": "Acceptance mapping", "ja": "受け入れ基準との対応"},
    "outOfScope": {"zh": "不做什么", "en": "Out of scope", "ja": "対象外"},
    # review
    "verdict": {"zh": "结论", "en": "Verdict", "ja": "結論"},
    "findings": {"zh": "问题清单", "en": "Findings", "ja": "指摘一覧"},
    "basis": {"zh": "依据", "en": "Basis", "ja": "根拠"},
    # progress
    "checklist": {"zh": "检查清单", "en": "Checklist", "ja": "チェックリスト"},
    "completed": {"zh": "已完成", "en": "Completed", "ja": "完了済み"},
    "blockers": {"zh": "卡点", "en": "Blockers", "ja": "障害"},
    # issue
    "problem": {"zh": "问题", "en": "Problem", "ja": "問題"},
    "impact": {"zh": "影响", "en": "Impact", "ja": "影響"},
    "reproduce": {"zh": "复现", "en": "Steps to reproduce", "ja": "再現手順"},
    "cause": {"zh": "原因", "en": "Cause", "ja": "原因"},
    "scope": {"zh": "范围", "en": "Scope", "ja": "範囲"},
    "notes": {"zh": "注意事项", "en": "Cautions", "ja": "注意事項"},
    "direction": {"zh": "修复方向", "en": "Fix direction", "ja": "修正方針"},
    # Issue 文件旧版式另有的小节(domain/issue_sections.py，读取旧文件用)
    "expected": {"zh": "期望与实际", "en": "Expected and actual", "ja": "期待される動作と実際の動作"},
    "fullEvidence": {"zh": "完整证据", "en": "Full evidence", "ja": "証拠一覧"},
    "meta": {"zh": "元信息", "en": "Metadata", "ja": "メタ情報"},
    "related": {"zh": "关联", "en": "Related", "ja": "関連"},
    "requirement": {"zh": "需求", "en": "Requirement", "ja": "要件"},
}

# 渲染时写入的固定文字
TEXTS: dict[str, dict[str, str]] = {
    "none": {"zh": "无", "en": "None", "ja": "なし"},
    "recommendation": {"zh": "推荐", "en": "Recommendation", "ja": "推奨"},
    "reason": {"zh": "理由", "en": "Reason", "ja": "理由"},
}


@dataclass(frozen=True)
class DocumentType:
    """一种交接文档：「内容」的固定小节(按顺序，全部必需)、数据块(标签到所在小节键)与头信息的 schema。"""

    kind: str
    purpose: str
    sections: tuple[str, ...]
    blocks: Mapping[str, str] = field(default_factory=dict)
    required_blocks: frozenset[str] = frozenset()
    header_schema: str = "handoff/document.schema.json"

    @property
    def schema(self) -> str:
        """数据块的 schema：每个标签是其中 `$defs` 的一项。"""
        return f"handoff/types/{self.kind}.schema.json"


TYPES: dict[str, DocumentType] = {
    doc.kind: doc for doc in (
        DocumentType("task", "上游向下游下达任务", ("goal", "inputs", "constraints", "acceptance", "deliverables"),
                     {"acceptance": "acceptance"}),
        DocumentType("result", "下游回报执行结果", ("done", "outputs", "evidence", "deviations"), {"checks": "evidence"}),
        DocumentType("finding", "采集产出的问题", ("symptom", "location", "evidence", "severityHint"),
                     {"locations": "location"}, frozenset({"locations"})),
        DocumentType("decision", "请求用户或上级拍板", ("background", "options", "recommendation"),
                     {"options": "options"}, frozenset({"options"})),
        DocumentType("plan", "修复或实现计划", ("approach", "steps", "acceptanceMapping", "outOfScope"),
                     {"files": "steps"}, frozenset({"files"})),
        DocumentType("review", "评审意见", ("verdict", "findings", "basis"), {"issues": "findings"}, frozenset({"issues"})),
        DocumentType("progress", "长任务的进展", ("checklist", "completed", "blockers"),
                     {"checklist": "checklist"}, frozenset({"checklist"})),
        DocumentType("issue", "本地 Issue", ("problem", "impact", "reproduce", "cause", "scope", "notes", "acceptance",
                                            "direction"), header_schema="handoff/frontmatter/issue.schema.json"),
    )
}


class UnknownKind(LookupError):
    """没有登记的文档类型。"""


def get(kind: str) -> DocumentType:
    if kind not in TYPES:
        raise UnknownKind(f"没有登记的文档类型：{kind}")
    return TYPES[kind]


def heading(key: str, code: str) -> str:
    return language.pick(HEADINGS[key], code)


def text(key: str, code: str) -> str:
    return language.pick(TEXTS[key], code)


def aliases(keys: tuple[str, ...]) -> dict[str, str]:
    """这些键在任何一种语言下的标题到键的映射。"""
    return {title: key for key in keys for title in HEADINGS[key].values()}
