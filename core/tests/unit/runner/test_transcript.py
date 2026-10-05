from datetime import datetime, timezone

import pytest

from tightrein.contracts import validate
from tightrein.domain.clock import FixedClock
from tightrein.observability.redact import REDACTED, Redactor
from tightrein.runner import transcript
from tightrein.runner.result import Usage
from tightrein.runner.transcript import EventDraft, TranscriptWriter

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
RUN = "R-20261005-030000-triage"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "transcripts" / "claim-verifier-P-0042.jsonl"


def writer(path):
    return TranscriptWriter(path, Redactor(), FixedClock(NOW), run_id=RUN, role="claim-verifier", subject_id="P-0042")


def test_events_follow_the_schema(path):
    target = writer(path)
    target.begin_call()
    drafts = [
        EventDraft("session-start", "system", session_id="s1"),
        EventDraft("message", "assistant", text="先读代码"),
        EventDraft("tool-call", "assistant", tool_name="Read", tool_input={"file_path": "src/A.cs"}, tool_call_id="t1"),
        EventDraft("tool-result", "user", tool_call_id="t1", tool_output="class A {}", is_error=False),
        EventDraft("usage", "system", usage=Usage(10, 2, None, 0.01, False),
                   timestamp=datetime(2026, 10, 5, 3, 1, tzinfo=timezone.utc)),
    ]
    written = target.write_all(drafts, tool="claude", model="claude-opus", session_id=None)
    assert [event["seq"] for event in written] == [1, 2, 3, 4, 5]
    assert written[1]["sessionId"] is None and written[0]["sessionId"] == "s1"
    assert written[0]["timestamp"] == "2026-10-05T03:00:00Z"
    assert written[4]["timestamp"] == "2026-10-05T03:01:00Z"
    for event in transcript.read(path):
        assert validate.validate("runner/transcript-event.schema.json", event) == []
    assert [event["toolName"] for event in target.tool_calls()] == ["Read"]


def test_calls_append_to_the_same_file(path):
    first = writer(path)
    first.begin_call()
    first.write(EventDraft("message", "assistant", text="第一次"), tool="codex", model=None, session_id="a")
    second = writer(path)
    assert second.begin_call() == 2
    event = second.write(EventDraft("message", "assistant", text="重试"), tool="codex", model=None, session_id="a")
    assert (event["seq"], event["attempt"]) == (2, 2)
    assert second.tool_calls() == []
    assert len(transcript.read(path)) == 2


def test_content_is_redacted_and_long_output_is_truncated(path):
    redactor = Redactor()
    redactor.register("Pa55-w0rd!")
    target = TranscriptWriter(path, redactor, FixedClock(NOW), run_id=RUN, role="claim-verifier", subject_id="P-0042")
    long_output = "line of output\n" * 1500 + "Pa55-w0rd!"
    event = target.write(EventDraft("tool-result", "user", tool_call_id="t1", tool_output=long_output),
                         tool="claude", model=None, session_id=None)
    assert len(event["toolOutput"]) == 16384
    assert event["toolOutput"].endswith(transcript.TRUNCATED)
    call = target.write(EventDraft("tool-call", "assistant", tool_name="Bash", tool_call_id="t2",
                                   tool_input={"command": "curl -H 'Authorization: Bearer abcdefghijklmnop'",
                                               "password": "x"}), tool="claude", model=None, session_id=None)
    assert call["toolInput"]["password"] == REDACTED
    assert "abcdefghijklmnop" not in call["toolInput"]["command"]
    assert call["attempt"] == 1


def test_unknown_lines_are_kept():
    draft = transcript.unknown('{"type": "future_event"}')
    assert (draft.type, draft.actor, draft.text) == ("message", "system", '{"type": "future_event"}')
    assert transcript.truncate(None, 16384) is None
    assert len(transcript.truncate("x" * 1000, 5)) <= 5
