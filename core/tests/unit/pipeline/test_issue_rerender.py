"""issue rerender：用现有分诊数据按当前模板重写本地正文与标题(旧版式转为交接文档版式)，保留历史，不调用模型。"""

from triage_world import make_triage_world, store_triaged

from tightrein.domain.enums import RunStage, Severity
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.store.files import handoff_files, issue_files
from tightrein.store.repos import handoffs, issues


def service(world):
    return IssueService(IssueDeps(world.layout, world.config, world.conn, world.clock, world.events,
                                  snapshot=world.worktree))


def legacy_body(path):
    """把文件换成旧模板的正文(结论、证据……)，模拟本计划之前建的 Issue。"""
    document = issue_files.read(path)
    history = document.body.split("## 历史", 1)[1]
    return "# 旧标题\n\n## 结论\n\n旧结论\n\n## 证据\n\n- 旧\n\n## 关联\n\n- P-0001 旧关联\n\n## 历史" + history


def test_rerender_rewrites_the_body_keeps_related_and_history_and_lists_missing_fields(tmp_path):
    world = make_triage_world(tmp_path)
    store_triaged(world)
    service(world).create()
    record = issues.get(world.conn, "0001")
    path = world.layout.root / record.path
    document = issue_files.read(path)
    issue_files.write(world.conn, world.layout, issue_files.IssueDocument(document.issue, legacy_body(path),
                                                                          document.run_id))
    result = service(world).rerender(["0001"])
    [item] = result.items
    assert item.rewritten and "title" in item.missing and result.mirror is None
    text = issue_files.read(path).body
    assert "### 问题" in text and "## 证据" not in text and "### 范围" in text and "### 注意事项" in text
    assert "- `P-0001` " in text.split("## 引用")[1] and "按当前模板重新渲染" in text.split("## 历史")[1]
    assert "创建(运行" in text.split("## 历史")[1]


def test_rerender_uses_the_report_title_and_skips_manual_issues(tmp_path):
    world = make_triage_world(tmp_path)
    store_triaged(world)
    service(world).create()
    found = handoffs.get(world.conn, RunStage.TRIAGE, "P-0001")
    document = handoff_files.read(world.layout.root / found.path)
    document["outputs"]["report"] = {
        "title": "[订单] 其他公司的订单查询返回 500", "summary": "查询失败。", "steps": ["登录", "查询"],
        "expected": "403", "actual": "500", "acceptance": ["返回 403"], "severity": "P2", "severityReason": "非核心"}
    (world.layout.root / found.path).write_text(__import__("json").dumps(document, ensure_ascii=False),
                                                encoding="utf-8")
    manual = service(world).create_manual("导出", "导出 CSV。", Severity.P2)
    before = (world.layout.root / manual.path).read_text(encoding="utf-8")
    result = service(world).rerender()
    # 用户需求的 Issue 已是交接文档版式时不改正文
    assert [(item.issue_id, item.rewritten, item.missing) for item in result.items] == [
        ("0001", True, []), (manual.issue.id, False, [])]
    assert issues.get(world.conn, "0001").issue.title == "[订单] 其他公司的订单查询返回 500"
    assert (world.layout.root / manual.path).read_text(encoding="utf-8") == before
    assert "- [ ] 返回 403" in issue_files.read(world.layout.root / issues.get(world.conn, "0001").path).body


def test_rerender_converts_a_legacy_manual_issue(tmp_path):
    world = make_triage_world(tmp_path)
    manual = service(world).create_manual("导出", "导出 CSV。", Severity.P2)
    path = world.layout.root / manual.path
    legacy = ("# 导出\n\n## 需求\n\n导出 CSV。\n\n## 验收标准\n\n- 得到 CSV 文件\n\n## 关联\n\n无\n\n"
              "## 历史\n\n- 2026-10-01 10:00(JST) 创建\n")
    issue_files.write(world.conn, world.layout, issue_files.IssueDocument(issue_files.read(path).issue, legacy))
    [item] = service(world).rerender([manual.issue.id]).items
    text = issue_files.read(path).body
    assert item.rewritten and "### 问题\n\n导出 CSV。" in text and "- [ ] 得到 CSV 文件" in text
    assert "- 2026-10-01 10:00(JST) 创建" in text and "## 需求" not in text
