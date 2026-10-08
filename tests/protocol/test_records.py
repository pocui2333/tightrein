import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import tightrein
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.records import EventLog, versions
from tightrein.protocol.security import Redactor

AT = datetime(2026, 10, 5, 3, 0, 5, tzinfo=UTC)
RUN = "R-20261005T030000Z-implement"


def log(tmp_path: Path, redactor: Redactor | None = None) -> EventLog:
    return EventLog(tmp_path / "data" / "runs" / RUN / "events.jsonl", redactor or Redactor(), FixedClock(AT))


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_events_are_appended_one_line_each(tmp_path):
    events = log(tmp_path)
    events.emit(run=RUN, subject="0018", point="implement.code", kind="action", summary="开始编码\n第二行",
                refs={"handoff": "issues/0018/35-implement.code-handoff.json"})
    events.emit(run=RUN, subject=None, point="implement", kind="effect", summary="运行结束")
    assert lines(events.path) == [
        {"at": "2026-10-05T03:00:05Z", "run": RUN, "subject": "0018", "point": "implement.code", "kind": "action",
         "summary": "开始编码\n第二行", "refs": {"handoff": "issues/0018/35-implement.code-handoff.json"}},
        {"at": "2026-10-05T03:00:05Z", "run": RUN, "subject": None, "point": "implement", "kind": "effect",
         "summary": "运行结束", "refs": {}},
    ]
    assert events.failures == []


def test_events_are_redacted_before_writing(tmp_path):
    redactor = Redactor()
    redactor.register("Pa55-w0rd!")
    events = log(tmp_path, redactor)
    key = "0018:implement.deliver:push:13812345678abcdef"
    events.emit(run=RUN, subject="0018", point="implement.deliver", kind="action",
                summary="登录用 Pa55-w0rd! 失败，手机 13812345678", refs={"idempotency": key})
    written = events.path.read_text(encoding="utf-8")
    assert "Pa55-w0rd!" not in written and "手机 13812345678" not in written
    event = lines(events.path)[0]
    assert event["summary"] == "登录用 [REDACTED:secret] 失败，手机 [REDACTED:phone]"
    assert (event["run"], event["refs"]) == (RUN, {"idempotency": key})


def test_unknown_kinds_are_a_programming_error(tmp_path):
    with pytest.raises(ValueError, match="事件种类"):
        log(tmp_path).emit(run=RUN, subject=None, point="implement", kind="note", summary="x")


def test_write_failures_are_recorded_not_raised(tmp_path):
    events = log(tmp_path)
    events.path.parent.parent.mkdir(parents=True)
    events.path.parent.write_text("不是目录", encoding="utf-8")
    events.emit(run=RUN, subject=None, point="implement", kind="trigger", summary="开始")
    assert len(events.failures) == 1
    assert events.failures[0].path == events.path
    assert events.failures[0].reason.startswith(("FileExistsError", "NotADirectoryError"))


WRITER = """
import sys
from datetime import datetime, timezone
from pathlib import Path

from tightrein.protocol.naming import FixedClock
from tightrein.protocol.records import EventLog
from tightrein.protocol.security import Redactor

path, writer, count = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
events = EventLog(path, Redactor(), FixedClock(datetime(2026, 10, 5, tzinfo=timezone.utc)))
for number in range(count):
    events.emit(run="R-20261005T000000Z-implement", subject=writer, point="implement", kind="action",
                summary=f"{writer}-{number}-" + "x" * 3000)
assert events.failures == [], events.failures
"""


def test_processes_writing_the_same_log_do_not_interleave_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    source = str(Path(tightrein.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (source, os.environ.get("PYTHONPATH"))))}
    writers = [subprocess.Popen([sys.executable, "-c", WRITER, str(path), f"w{index}", "200"], env=env)
               for index in range(4)]
    assert [writer.wait(timeout=60) for writer in writers] == [0] * 4
    written = lines(path)
    assert len(written) == 800
    for index in range(4):
        summaries = [event["summary"] for event in written if event["subject"] == f"w{index}"]
        assert summaries == [f"w{index}-{number}-" + "x" * 3000 for number in range(200)]


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false",
         "-c", "core.hooksPath=/dev/null", *args], cwd=repo, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


@pytest.fixture
def tool_root(tmp_path):
    root = tmp_path / "tightrein"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_versions_take_the_commit_of_the_tool(tool_root):
    head = git(tool_root, "rev-parse", "HEAD")
    found = versions(tool_root, "s-hash", prompt_hash="p-hash", tool="claude", tool_version="2.1.0", model="opus")
    assert (found.tightrein, found.settings, found.prompt, found.tool, found.tool_version, found.model) == (
        head, "s-hash", "p-hash", "claude", "2.1.0", "opus")


def test_versions_read_packed_refs_detached_heads_and_worktrees(tool_root, tmp_path):
    head = git(tool_root, "rev-parse", "HEAD")
    git(tool_root, "pack-refs", "--all")
    assert versions(tool_root, "s", prompt_hash=None, tool=None, tool_version=None, model=None).tightrein == head
    worktree = tmp_path / "wt"
    git(tool_root, "worktree", "add", "-q", "-b", "other", str(worktree))
    assert versions(worktree, "s", prompt_hash=None, tool=None, tool_version=None, model=None).tightrein == head
    git(tool_root, "checkout", "-q", "--detach")
    assert versions(tool_root, "s", prompt_hash=None, tool=None, tool_version=None, model=None).tightrein == head


def test_versions_outside_a_repository_have_no_commit(tmp_path):
    assert versions(tmp_path, "s", prompt_hash=None, tool=None, tool_version=None, model=None).tightrein is None
