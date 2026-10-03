import threading
from datetime import date

import pytest
import yaml

from tightrein.domain.enums import Probe
from tightrein.domain.suppression import SuppressionRule
from tightrein.store.files import suppressions
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.files.suppressions import SuppressionFileError

TODAY = date(2026, 10, 5)


@pytest.fixture
def layout(tmp_path):
    return WorkspaceLayout(tmp_path / "sample")


def by_fingerprint(value="a1b2c3d4e5f60718", days=30, reason="上游已校验"):
    return SuppressionRule.for_fingerprint(value, reason, TODAY, days)


def by_message():
    return SuppressionRule("已知的第三方告警", TODAY, date(2026, 12, 31), probe=Probe.PLATFORM_ERRORS,
                           message_pattern=r"^Warning: .*deprecated")


def test_missing_file_has_no_rules(layout):
    assert suppressions.read(layout.suppressions()) == []


def test_add_rule_writes_a_readable_file(layout, clock):
    assert suppressions.add_rule(layout, by_fingerprint(), clock) == [by_fingerprint()]
    text = layout.suppressions().read_text(encoding="utf-8")
    assert "addedOn: '2026-10-05'" in text
    assert yaml.safe_load(text)["rules"][0]["expiresOn"] == "2026-11-04"
    assert suppressions.read(layout.suppressions()) == [by_fingerprint()]
    assert not layout.archive_dir().exists()


def test_each_write_backs_up_the_previous_version(layout, clock):
    suppressions.add_rule(layout, by_fingerprint(), clock)
    first = layout.suppressions().read_text(encoding="utf-8")
    suppressions.add_rule(layout, by_message(), clock)
    second = layout.suppressions().read_text(encoding="utf-8")
    suppressions.add_rule(layout, by_fingerprint("b" * 16), clock)
    assert layout.suppressions_backup(clock.now()).read_text(encoding="utf-8") == first
    assert layout.suppressions_backup(clock.now(), 1).read_text(encoding="utf-8") == second
    assert suppressions.read(layout.suppressions()) == [by_fingerprint(), by_message(), by_fingerprint("b" * 16)]


def test_the_same_fingerprint_replaces_the_rule(layout, clock):
    suppressions.add_rule(layout, by_fingerprint(), clock)
    suppressions.add_rule(layout, by_message(), clock)
    renewed = by_fingerprint(days=90, reason="再次判为误报")
    assert suppressions.add_rule(layout, renewed, clock) == [renewed, by_message()]


@pytest.mark.parametrize("text, message", [
    ("rules: [1\n", "无法解析"),
    ("- a\n", "顶层键"),
    ("rules: {}\n", "顶层键"),
    ("rules:\n- match: {fingerprint: x}\n  reason: r\n  addedOn: '2026-10-05'\n", "第 1 条规则：规则的键"),
    ("rules:\n- match: {fingerprint: x, probe: alerts}\n  reason: r\n  addedOn: '2026-10-05'\n"
     "  expiresOn: '2026-10-06'\n", "match 只能是"),
    ("rules:\n- match: {fingerprint: x}\n  reason: r\n  addedOn: 2026/10/05\n  expiresOn: '2026-10-06'\n",
     "Invalid isoformat"),
    ("rules:\n- match: {probe: alerts, messagePattern: '(' }\n  reason: r\n  addedOn: '2026-10-05'\n"
     "  expiresOn: '2026-10-06'\n", "missing"),
    ("rules:\n- match: {probe: nope, messagePattern: x}\n  reason: r\n  addedOn: '2026-10-05'\n"
     "  expiresOn: '2026-10-06'\n", "nope"),
    ("rules:\n- match: {fingerprint: x}\n  reason: ''\n  addedOn: '2026-10-05'\n  expiresOn: '2026-10-06'\n",
     "reason"),
])
def test_invalid_files(text, message):
    with pytest.raises(SuppressionFileError, match=message):
        suppressions.parse(text)


def test_unquoted_dates_are_accepted_as_strings():
    text = "rules:\n- match: {fingerprint: x}\n  reason: r\n  addedOn: 2026-10-05\n  expiresOn: 2026-10-06\n"
    assert suppressions.parse(text)[0].expires_on == date(2026, 10, 6)


def test_every_invalid_rule_is_listed_and_nothing_is_written(layout, clock):
    path = layout.suppressions()
    path.parent.mkdir(parents=True)
    original = "rules:\n- match: {fingerprint: x}\n- reason: r\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(SuppressionFileError, match="第 1 条规则") as error:
        suppressions.add_rule(layout, by_fingerprint(), clock)
    assert "第 2 条规则" in str(error.value)
    assert path.read_text(encoding="utf-8") == original
    assert not layout.archive_dir().exists()


def test_a_write_that_does_not_read_back_is_rolled_back(layout, clock, monkeypatch):
    suppressions.add_rule(layout, by_fingerprint(), clock)
    before = layout.suppressions().read_text(encoding="utf-8")
    monkeypatch.setattr(suppressions, "render", lambda rules: "rules: []\n")
    with pytest.raises(SuppressionFileError, match="已恢复"):
        suppressions.add_rule(layout, by_message(), clock)
    assert layout.suppressions().read_text(encoding="utf-8") == before


def test_concurrent_writers_do_not_lose_rules(layout, clock):
    threads = [
        threading.Thread(target=suppressions.add_rule, args=(layout, by_fingerprint(f"{number:016x}"), clock))
        for number in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stored = suppressions.read(layout.suppressions())
    assert sorted(rule.fingerprint for rule in stored) == [f"{number:016x}" for number in range(6)]
