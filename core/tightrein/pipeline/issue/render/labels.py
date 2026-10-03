"""Issue 正文、GitHub 镜像与关键节点评论中的固定文字，按 project.language 取 zh、en、ja(其他语言取英文)。

小节标题见 domain/issue_sections.py；模型写的叙述由提示中的「输出语言」决定，不经这里。
"""

from __future__ import annotations

from typing import Any

from tightrein.domain import language
from tightrein.domain.enums import CloseReason, IssueEvent, IssueStatus, TaskType

MISSING = "—"

TEXTS: dict[str, dict[str, str]] = {
    # 正文
    "consequence": {"zh": "后果", "en": "Consequence", "ja": "影響"},
    "roles": {"zh": "受影响的角色", "en": "Affected roles", "ja": "影響を受けるロール"},
    "data": {"zh": "受影响的数据", "en": "Affected data", "ja": "影響を受けるデータ"},
    "scope": {"zh": "范围(调用点)", "en": "Scope (call sites)", "ja": "範囲(呼び出し箇所)"},
    "reproduce": {"zh": "复现命令", "en": "Reproduce command", "ja": "再現コマンド"},
    "step": {"zh": "失败步骤", "en": "Failed step", "ja": "失敗したステップ"},
    "screenshot": {"zh": "截图", "en": "Screenshot", "ja": "スクリーンショット"},
    "trace": {"zh": "trace", "en": "Trace", "ja": "トレース"},
    "expected": {"zh": "期望", "en": "Expected", "ja": "期待"},
    "actual": {"zh": "实际", "en": "Actual", "ja": "実際"},
    "rootCause": {"zh": "根因位置", "en": "Root cause", "ja": "根本原因の箇所"},
    "trigger": {"zh": "触发条件", "en": "Trigger", "ja": "発生条件"},
    "entries": {"zh": "调用链入口", "en": "Entry points", "ja": "呼び出し元の入口"},
    "introduced": {"zh": "引入", "en": "Introduced by", "ja": "混入コミット"},
    "introducedItem": {"zh": "{commit}(作者 {author}，PR {pr})", "en": "{commit} (author {author}, PR {pr})",
                       "ja": "{commit}(作成者 {author}、PR {pr})"},
    "estimated": {"zh": "预估改动", "en": "Estimated changes", "ja": "変更見込み"},
    "decide": {"zh": "需用户定夺", "en": "Needs a decision by the maintainer", "ja": "メンテナーの判断が必要"},
    "discuss": {"zh": "需要先与代码作者讨论", "en": "Discuss with the code author first", "ja": "先にコード作成者と相談"},
    "flag.design": {"zh": "根因在设计本身", "en": "Root cause is in the design", "ja": "原因が設計にある"},
    "flag.dataStructure": {"zh": "要动数据结构或存量数据", "en": "Changes data structures or existing data",
                           "ja": "データ構造または既存データの変更が必要"},
    "flag.publicContract": {"zh": "会改变公共实现或接口契约", "en": "Changes a shared implementation or API contract",
                            "ja": "共通実装または API 契約が変わる"},
    "item": {"zh": "项", "en": "Item", "ja": "項目"},
    "value": {"zh": "值", "en": "Value", "ja": "値"},
    "severity": {"zh": "严重度", "en": "Severity", "ja": "重大度"},
    "treatment": {"zh": "处理标签", "en": "Treatment", "ja": "対応区分"},
    "treatment.immediate": {"zh": "立即修", "en": "Fix now", "ja": "即時修正"},
    "treatment.scheduled": {"zh": "排期修", "en": "Scheduled", "ja": "計画修正"},
    "treatment.observe": {"zh": "观察", "en": "Observe", "ja": "経過観察"},
    "treatment.wont-fix": {"zh": "不修", "en": "Won't fix", "ja": "修正しない"},
    "taskType": {"zh": "任务类型", "en": "Task type", "ja": "タスク種別"},
    "sizeTier": {"zh": "规模档", "en": "Size tier", "ja": "規模"},
    "source": {"zh": "来源", "en": "Source", "ja": "検出元"},
    "ids": {"zh": "本地编号", "en": "Local IDs", "ja": "ローカル番号"},
    "commit": {"zh": "取证 commit", "en": "Evidence commit", "ja": "調査時のコミット"},
    "issueId": {"zh": "Issue {issue}，问题 {problems}", "en": "Issue {issue}, problems {problems}",
                "ja": "Issue {issue}、問題 {problems}"},
    "manualAcceptance": {
        "zh": "未单独列出：修复计划按「需求」一节逐条对应；可用 tightrein issue edit 补充，每条以「- 」开头。",
        "en": "Not listed: the fix plan follows the Requirement section item by item; add criteria with "
              "tightrein issue edit, one per line starting with \"- \".",
        "ja": "個別には挙げていません。修正計画は「要件」に沿います。tightrein issue edit で 1 行ずつ「- 」で追加できます。"},
    "manualRelated": {"zh": "无(用户需求，不关联问题)", "en": "None (user request, no linked problem)",
                      "ja": "なし(ユーザー要望のため問題と関連付けない)"},
    # 验收标准
    "accept.apiFuzz": {"zh": "api-fuzz 对 `{location}` 的 `{check}` 检查以 `{role}` 身份通过",
                       "en": "api-fuzz check `{check}` on `{location}` passes as `{role}`",
                       "ja": "api-fuzz の `{check}` チェックが `{location}` で `{role}` として通る"},
    "accept.anonymous": {"zh": "未登录", "en": "anonymous", "ja": "未ログイン"},
    "accept.observed": {"zh": "部署后的观察期与之后的覆盖运行中不再出现指纹为 `{fingerprint}` 的问题",
                        "en": "No problem with fingerprint `{fingerprint}` appears during the post-deploy observation "
                              "window and later covering runs",
                        "ja": "デプロイ後の観察期間とその後の対象の実行でフィンガープリント `{fingerprint}` の問題が出ない"},
    "accept.static": {"zh": "静态巡检的 `{check}` 规则在 `{location}` 不再命中",
                      "en": "Static check `{check}` no longer matches at `{location}`",
                      "ja": "静的チェック `{check}` が `{location}` で検出されない"},
    "accept.refuted": {"zh": "对原发现重新取证，判定为不成立", "en": "Re-examining the original finding refutes it",
                       "ja": "元の検出を再調査すると不成立と判定される"},
    "accept.regression": {"zh": "修复环节建立的复现检查通过", "en": "The reproduction check created during the fix passes",
                          "ja": "修正時に作成した再現チェックが通る"},
    "accept.reproduce": {"zh": "本 Issue 的复现检查在修复后通过", "en": "This issue's reproduction check passes after the fix",
                         "ja": "修正後、この Issue の再現チェックが通る"},
    "accept.reproTest": {"zh": "复现测试在修复前失败、修复后通过",
                         "en": "The reproduction test fails before the fix and passes after it",
                         "ja": "再現テストが修正前に失敗し、修正後に通る"},
    "accept.reproTestSkipped": {"zh": "复现测试：本类型不写(fix.repro.skipTypes)",
                                "en": "Reproduction test: not written for this task type (fix.repro.skipTypes)",
                                "ja": "再現テスト：このタスク種別では書かない(fix.repro.skipTypes)"},
    "accept.existingTests": {"zh": "现有测试全部通过", "en": "All existing tests pass", "ja": "既存のテストがすべて通る"},
    "accept.keep": {"zh": "保持不变：{item}", "en": "Unchanged: {item}", "ja": "変更しない：{item}"},
    "accept.keepDefault": {"zh": "除本 Issue 描述的问题外，现有行为保持不变",
                           "en": "Apart from the problem described here, existing behavior stays the same",
                           "ja": "この Issue の問題以外の既存の動作は変わらない"},
    # 范围与注意事项
    "scopeFiles": {"zh": "可能涉及的文件", "en": "Files likely involved", "ja": "関係しそうなファイル"},
    "outOfScope": {"zh": "不在本次范围内", "en": "Out of scope", "ja": "今回の対象外"},
    "mustKeep": {"zh": "不能改", "en": "Must not change", "ja": "変更不可"},
    "findings": {"zh": "发现报告", "en": "Findings report", "ja": "検出レポート"},
    "consequenceShort": {"zh": "后果：{text}", "en": "Consequence: {text}", "ja": "影響：{text}"},
    "next.approve": {"zh": "审阅并放行：tightrein issue approve {n}",
                     "en": "Review and approve: tightrein issue approve {n}",
                     "ja": "確認して承認：tightrein issue approve {n}"},
    "next.fix": {"zh": "修复：tightrein fix start {n}", "en": "Fix: tightrein fix start {n}",
                 "ja": "修正：tightrein fix start {n}"},
    # GitHub 镜像
    "mirror.cause": {"zh": "原因", "en": "Cause", "ja": "原因"},
    "mirror.problem": {"zh": "问题", "en": "Problem", "ja": "問題"},
    "mirror.method": {"zh": "方法", "en": "Method", "ja": "解決方法"},
    "mirror.reproduce": {"zh": "复现步骤", "en": "Steps to Reproduce", "ja": "再現手順"},
    "mirror.files": {"zh": "涉及文件", "en": "Files involved", "ja": "関係するファイル"},
    "evidenceSummary": {"zh": "展开 {count} 条证据", "en": "Show {count} evidence items", "ja": "証拠 {count} 件を表示"},
    "footer": {"zh": "这是 tightrein 本地 Issue {issue} 的镜像：开关状态以 GitHub 为准，在这里关闭或重新打开会同步回本地；"
                     "标题、正文、标签与子 Issue 关系以本地为准，在这里的编辑会在下次同步时被覆盖，评论不会同步回本地。",
               "en": "Mirror of tightrein local issue {issue}: open/closed state follows GitHub, so closing or "
                     "reopening here is synced back. Title, description, labels and sub-issue links follow the "
                     "local issue and edits here are overwritten on the next sync; comments are not synced back.",
               "ja": "tightrein のローカル Issue {issue} のミラーです。オープン・クローズは GitHub が正で、ここでの操作は"
                     "ローカルに反映されます。タイトル・本文・ラベル・サブ Issue の関係はローカルが正で、ここでの編集は次回の"
                     "同期で上書きされます。コメントは反映されません。"},
    "typeLabel": {"zh": "类型：{name}", "en": "Type: {name}", "ja": "種別：{name}"},
    # 评论
    "closed": {"zh": "本地 Issue 已关闭，原因：{reason}", "en": "Local issue closed: {reason}",
               "ja": "ローカル Issue をクローズしました(理由：{reason})"},
    "state": {"zh": "本地 Issue {issue}，当前状态「{status}」。", "en": "Local issue {issue}, now \"{status}\".",
              "ja": "ローカル Issue {issue}、現在の状態「{status}」。"},
    "approvedManual": {"zh": "放行：申请建修复分支", "en": "Approved: requesting a fix branch",
                       "ja": "承認：修正ブランチを申請"},
    "planConfirmed": {"zh": "修复计划已确认，开始实施", "en": "Fix plan confirmed; implementation started",
                      "ja": "修正計画を確定し、実装を開始"},
}

STATUS: dict[IssueStatus, dict[str, str]] = {
    IssueStatus.NEEDS_DECISION: {"en": "Needs decision", "ja": "判断待ち"},
    IssueStatus.TODO: {"en": "To fix", "ja": "修正待ち"},
    IssueStatus.IN_PROGRESS: {"en": "In progress", "ja": "対応中"},
    IssueStatus.PENDING_MERGE: {"en": "Pending merge", "ja": "マージ待ち"},
    IssueStatus.DONE: {"en": "Done", "ja": "完了"},
    IssueStatus.CANCELLED: {"en": "Cancelled", "ja": "取り消し"},
}
TASK_TYPES: dict[TaskType, dict[str, str]] = {
    TaskType.BUG: {"en": "Bug", "ja": "不具合"},
    TaskType.SECURITY: {"en": "Security", "ja": "セキュリティ"},
    TaskType.DATA: {"en": "Data", "ja": "データ"},
    TaskType.FRONTEND: {"en": "Frontend", "ja": "フロントエンド"},
    TaskType.FEATURE: {"en": "Feature", "ja": "機能"},
    TaskType.REFACTOR: {"en": "Refactoring", "ja": "リファクタリング"},
    TaskType.DEPENDENCY: {"en": "Dependency", "ja": "依存関係"},
    TaskType.DOCS_CONFIG: {"en": "Docs and config", "ja": "ドキュメント・設定"},
}
CLOSE_REASONS: dict[CloseReason, dict[str, str]] = {
    CloseReason.FIXED: {"en": "fixed", "ja": "修正済み"},
    CloseReason.FIX_REJECTED: {"en": "fix not accepted", "ja": "修正不採用"},
    CloseReason.WONT_FIX: {"en": "won't fix", "ja": "修正しない"},
    CloseReason.DUPLICATE: {"en": "duplicate", "ja": "重複"},
    CloseReason.NOT_A_BUG: {"en": "not a bug", "ja": "不具合ではない"},
}
EVENTS: dict[IssueEvent, dict[str, str]] = {
    IssueEvent.APPROVE: {"en": "Approved", "ja": "承認"},
    IssueEvent.PR_CREATED: {"en": "PR created", "ja": "PR 作成"},
    IssueEvent.PR_MERGED: {"en": "PR merged", "ja": "PR マージ"},
}


def text(key: str, code: str, **values: Any) -> str:
    found = language.pick(TEXTS[key], code)
    return found.format(**values) if values else found


def _labelled(table: dict[Any, dict[str, str]], member: Any, code: str) -> str:
    return language.pick({"zh": member.label, "en": member.value, **table.get(member, {})}, code)


def status(value: IssueStatus, code: str) -> str:
    return _labelled(STATUS, value, code)


def task_type(value: TaskType, code: str) -> str:
    """类型标签的说明：类型：<任务类型>。"""
    return text("typeLabel", code, name=_labelled(TASK_TYPES, value, code))


def close_reason(value: CloseReason, code: str) -> str:
    return _labelled(CLOSE_REASONS, value, code)


def event(value: IssueEvent, code: str) -> str:
    return _labelled(EVENTS, value, code)
