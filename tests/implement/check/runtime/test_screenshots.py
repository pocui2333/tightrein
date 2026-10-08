from __future__ import annotations

import struct
import zlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tightrein.agents.result import CallResult, CallStatus
from tightrein.implement.check.runtime import screenshots
from tightrein.implement.check.runtime.pages import Screenshot
from tightrein.implement.check.runtime.screenshots import CompareSettings
from tightrein.implement.check.runtime.verdict import Item, Result
from tightrein.implement.context import Decision
from tightrein.protocol.handoff import Status
from tightrein.protocol.naming import format_iso

SETTINGS = CompareSettings(pixel_ratio=0.1, channel_tolerance=8)


def png(rows: list[list[tuple[int, int, int]]], *, filter_type: int = 0) -> bytes:
    """8 位 RGB、不隔行的 PNG；filter_type 1 为 Sub，2 为 Up(验证去过滤)。"""
    height, width = len(rows), len(rows[0])
    raw = bytearray()
    previous = bytes(width * 3)
    for row in rows:
        line = bytes(value for pixel in row for value in pixel)
        if filter_type == 1:
            encoded = bytes((line[i] - (line[i - 3] if i >= 3 else 0)) & 0xFF for i in range(len(line)))
        elif filter_type == 2:
            encoded = bytes((line[i] - previous[i]) & 0xFF for i in range(len(line)))
        else:
            encoded = line
        raw += bytes([filter_type]) + encoded
        previous = line

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return screenshots.PNG_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw))) + \
        chunk(b"IEND", b"")


WHITE = [[(255, 255, 255)] * 4 for _ in range(4)]


def _with(changes: dict[tuple[int, int], tuple[int, int, int]]) -> list[list[tuple[int, int, int]]]:
    rows = [list(row) for row in WHITE]
    for (y, x), pixel in changes.items():
        rows[y][x] = pixel
    return rows


def test_pixel_comparison_tolerates_noise_and_decodes_filters(tmp_path: Path) -> None:
    base = tmp_path / "base.png"
    base.write_bytes(png(WHITE))
    noisy = tmp_path / "noisy.png"
    noisy.write_bytes(png(_with({(0, 0): (250, 252, 255)}), filter_type=1))  # 通道差 5，在容差内
    moved = tmp_path / "moved.png"
    moved.write_bytes(png(_with({(y, 1): (0, 0, 0) for y in range(4)}), filter_type=2))  # 一整列变黑：4/16
    assert screenshots.decode(png(WHITE, filter_type=1)) == screenshots.decode(png(WHITE))
    assert screenshots.same(noisy, base, SETTINGS)
    assert not screenshots.same(moved, base, SETTINGS)
    assert not screenshots.same(base, tmp_path / "missing.png", SETTINGS)
    other_size = tmp_path / "other.png"
    other_size.write_bytes(png([[(255, 255, 255)] * 2]))
    assert not screenshots.same(other_size, base, SETTINGS)
    assert screenshots.decode(b"GIF89a") is None


def _shots(tmp_path: Path) -> list[Screenshot]:
    found = []
    for name, rows in (("same", WHITE), ("changed", _with({(y, 1): (0, 0, 0) for y in range(4)}))):
        path = tmp_path / "test-results" / f"case-{name}" / "test-finished-1.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(png(rows))
        found.append(Screenshot(str(path), "/orders"))
    return found


def _fake_call(monkeypatch: pytest.MonkeyPatch, output: dict[str, Any] | None,
               status: CallStatus = CallStatus.OK) -> list[Any]:
    calls: list[Any] = []

    def call(params: Any, context: Any) -> CallResult:
        calls.append(params)
        return CallResult(status, "agy", "gemini", output=output)

    monkeypatch.setattr(screenshots, "call", call)
    return calls


def test_only_changed_screenshots_go_to_the_model(world: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shots = _shots(tmp_path)
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    for shot in shots:
        (baseline / f"{screenshots.key(shot)}.png").write_bytes(png(WHITE))
    calls = _fake_call(monkeypatch, {"analysis": "看过", "screenshots": [
        {"path": shots[1].path, "result": "issue", "reason": "第二列被遮挡"}]})
    review = screenshots.review(world.runtime, world.context(), shots, raw_dir=tmp_path, references=[baseline],
                                settings=SETTINGS)
    assert len(calls) == 1 and calls[0].read_paths == (Path(shots[1].path),)
    assert shots[0].path not in calls[0].prompt and shots[1].path in calls[0].prompt
    results = {item.evidence[0]: item.result for item in review.items}
    assert results == {shots[0].path: Result.PASSED, shots[1].path: Result.FAILED} and review.awaiting == []


def test_screenshots_are_reviewed_or_left_to_the_user(world: Any, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    shots = _shots(tmp_path)
    assert screenshots.review(world.runtime, world.context(), [], raw_dir=tmp_path, references=[],
                              settings=SETTINGS).items[0].result is Result.UNVERIFIED
    _fake_call(monkeypatch, None, CallStatus.FAILED)
    failed = screenshots.review(world.runtime, world.context(), shots, raw_dir=tmp_path, references=[],
                                settings=SETTINGS)
    assert sorted(failed.awaiting) == sorted(screenshots.key(shot) for shot in shots)
    _fake_call(monkeypatch, {"analysis": "x", "screenshots": [
        {"path": shots[0].path, "result": "unknown", "reason": "看不清"},
        {"path": "/elsewhere.png", "result": "ok", "reason": "不在清单里"}]})
    unsure = screenshots.review(world.runtime, world.context(), shots, raw_dir=tmp_path, references=[],
                                settings=SETTINGS)
    # 判不了的与漏看的都交用户；模型写的清单外路径不认
    assert sorted(unsure.awaiting) == sorted(screenshots.key(shot) for shot in shots)
    assert all(item.evidence[0] != "/elsewhere.png" for item in unsure.items)
    # 用户给了结论：下一次运行截图路径变了也按 key 认，不再调用模型
    waiting = world.handoff("implement.check", {"awaitingScreenshots": unsure.awaiting}, Status.PENDING)
    later = format_iso(world.clock.now() + timedelta(minutes=1))
    calls = _fake_call(monkeypatch, None)
    ok = screenshots.review(world.runtime, world.context(latest={"implement.check": waiting}, decisions=[
        Decision("implement.check", "approve", None, None, later)]), shots, raw_dir=tmp_path, references=[],
        settings=SETTINGS)
    assert calls == [] and {item.result for item in ok.items} == {Result.PASSED}
    issue = screenshots.review(world.runtime, world.context(latest={"implement.check": waiting}, decisions=[
        Decision("implement.check", "reject", None, "按钮重叠", later)]), shots, raw_dir=tmp_path, references=[],
        settings=SETTINGS)
    assert {item.result for item in issue.items} == {Result.FAILED} and "按钮重叠" in (issue.items[0].reason or "")


def test_passed_screenshots_become_the_reference_for_the_next_round(tmp_path: Path) -> None:
    shots = _shots(tmp_path)
    items = [Item(f"screenshot:{shots[0].path}", "screenshots", Result.PASSED, None, (shots[0].path,)),
             Item(f"screenshot:{shots[1].path}", "screenshots", Result.FAILED, None, (shots[1].path,))]
    folder = tmp_path / "reviewed"
    screenshots.remember(shots, items, folder)
    assert [path.name for path in folder.iterdir()] == [f"{screenshots.key(shots[0])}.png"]
    assert screenshots.changed(shots, [folder], SETTINGS) == [shots[1]]
