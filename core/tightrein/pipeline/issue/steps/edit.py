"""用编辑器修改 Issue 文件(architecture/06 10.6)。

编辑后按 handoff/frontmatter/issue.schema.json 与必需章节(按小节键，任一语言的标题或旧标题均可；交接文档版式为「内容」
下的八个小节与历史，旧版式按 issue_sections.LEGACY_TRIAGE_KEYS 或 LEGACY_MANUAL_KEYS)校验，不合格时把逐条错误交给 retry 决定重新编辑还是放弃(放弃时恢复
原文件)；status 与 closeReason 只能经 approve、close、reopen 改变，编辑中改动它们视为不合格。通过后更新索引，
「历史」追加「用户编辑」一行与改动的节名，写一条 user-edited 事件。编辑器与询问由调用方注入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from tightrein.domain import issue_sections
from tightrein.domain.language import pick
from tightrein.pipeline.issue.render import issue as template
from tightrein.pipeline.issue.steps.transitions import USER, IssueEnv, record_of
from tightrein.store.files import issue_files
from tightrein.store.files.issue_files import IssueDocument, IssueFileError, content_hash
from tightrein.store.repos import issue_events, issues
from tightrein.store.repos.issue_events import USER_EDITED, IssueEventRecord
from tightrein.store.repos.issues import IssueRecord

STATUS_HINT = "status 与 closeReason 只能用 tightrein issue approve、close、reopen 修改"


@dataclass(frozen=True)
class EditResult:
    saved: bool
    sections: tuple[str, ...] = ()
    record: IssueRecord | None = None


def sections(body: str) -> dict[str, str]:
    return issue_sections.split(body)


def problems_of(path: Path, before: IssueDocument) -> tuple[IssueDocument | None, list[str]]:
    try:
        after = issue_files.read(path)
    except IssueFileError as error:
        return None, [str(error)]
    present = issue_sections.keys_in(after.body)
    if issue_sections.is_handoff(after.body):
        required = template.required_sections()
    else:
        required = issue_sections.LEGACY_MANUAL_KEYS if after.issue.is_manual else issue_sections.LEGACY_TRIAGE_KEYS
    errors = [f"缺少章节「{pick(issue_sections.HEADINGS[key], 'zh')}」" for key in required if key not in present]
    if (after.issue.status, after.issue.close_reason) != (before.issue.status, before.issue.close_reason):
        errors.append(STATUS_HINT)
    return after, errors


def edit(env: IssueEnv, issue_id: str, editor: Callable[[Path], None],
         retry: Callable[[list[str]], bool]) -> EditResult:
    record = record_of(env, issue_id)
    path = env.layout.root / record.path
    original = path.read_text(encoding="utf-8")
    before = issue_files.read(path)
    while True:
        editor(path)
        text = path.read_text(encoding="utf-8")
        if text == original:
            return EditResult(False)
        after, errors = problems_of(path, before)
        if not errors and after is not None:
            break
        if not retry(errors):
            path.write_text(original, encoding="utf-8")
            return EditResult(False)
    old, new = sections(before.body), sections(after.body)
    changed = tuple(name for name in new if old.get(name) != new[name])
    issues.save(env.conn, IssueRecord(after.issue, record.path, content_hash(text)))
    now = env.clock.now()
    body = template.append_history(after.body, now, f"用户编辑：{'、'.join(changed) or '元数据'}", env.zone)
    written = issue_files.write(env.conn, env.layout,
                                IssueDocument(replace(after.issue, updated_at=now), body, after.run_id))
    issue_events.append(env.conn, IssueEventRecord(issue_id, now, USER_EDITED, USER, note="、".join(changed) or None))
    return EditResult(True, changed, written)
