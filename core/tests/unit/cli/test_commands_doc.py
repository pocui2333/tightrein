import io
import json
from datetime import datetime, timezone

from cli_world import make_cli_world

from tightrein.cli import exit_codes
from tightrein.cli.main import main
from tightrein.domain.enums import DocumentStatus
from tightrein.domain.handoff.document import HandoffDocument, Header
from tightrein.store.files import documents

AT = datetime(2026, 10, 2, 2, 15, tzinfo=timezone.utc)


def write_progress(path, completed="建分支。"):
    document = HandoffDocument(
        Header("progress", "PG-0003", DocumentStatus.IN_PROGRESS, "fix", "user", "0003", AT, AT),
        "通道 B，正在出计划。", {"checklist": "见清单。", "completed": completed, "blockers": "无。"},
        {"checklist": [{"item": "出计划", "state": "pending", "owner": "system"}]})
    path.write_text(documents.render(document, "zh"), encoding="utf-8")


def call(world, *argv):
    out = io.StringIO()
    code = main([*argv, "--json"], world.externals(), stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
    return code, json.loads(out.getvalue())


def test_doc_check_passes_a_valid_document_without_a_workspace(tmp_path):
    world = make_cli_world(tmp_path)
    path = tmp_path / "progress.md"
    write_progress(path)
    code, values = call(world, "doc", "check", str(path))
    assert code == exit_codes.OK and values["result"] == []


def test_doc_check_takes_the_limits_from_the_workspace(tmp_path):
    world = make_cli_world(tmp_path, documents={"sectionMaxChars": {"completed": 3}})
    path = tmp_path / "progress.md"
    write_progress(path, completed="建分支与 worktree。")
    assert call(world, "doc", "check", str(path))[0] == exit_codes.OK
    code, values = call(world, "doc", "check", str(path), "--workspace", str(world.root))
    assert code == exit_codes.FAILED
    assert values["result"] == ["「已完成」有 14 个字符，超过上限 3；超出的部分放进单独的附件文件，正文只给引用与摘要"]


def test_doc_check_reports_a_missing_file_as_a_usage_error(tmp_path):
    world = make_cli_world(tmp_path)
    assert call(world, "doc", "check", str(tmp_path / "none.md"))[0] == exit_codes.USAGE
