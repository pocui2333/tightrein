"""截图必须有人看：先由程序像素比对，只把有变化的交给模型看(调用点 implement.check.screenshots)。

- 比对对象：同一页面同一用例在基准 commit 上的截图(baseline_dir，由准备阶段或之前的运行留下)，以及本 Issue 上一轮
  已看过且无问题的截图(reviewed_dir)；与任一个像素一致(容差内)即不再交给模型：布局没变，结论沿用；
- 先比文件内容，再比解压后的图像数据，都不同才逐像素比较(只支持 8 位 RGB、RGBA、不隔行的 PNG，即 Playwright 的
  截图；别的格式一律当作有变化)；
- 截图审查只判遮挡、错位、溢出这类布局问题；审查给不出结论(unknown)或调用失败(例如工具不能读图)时交用户以 ok 或问题
  说明给结论；巡检没产出受影响页面的截图记未验证。
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tightrein.agents.call import call, params_for
from tightrein.agents.result import CallResult, CallStatus
from tightrein.implement.check.runtime.pages import Screenshot
from tightrein.implement.check.runtime.verdict import Item, Result, unverified
from tightrein.implement.context import ImplementContext
from tightrein.prompts.build import build
from tightrein.protocol.handoff import Tokens, load_schema
from tightrein.protocol.naming import parse_iso
from tightrein.protocol.runtime import Runtime
from tightrein.settings.load import Settings
from tightrein.store.files.layout import WorkspaceLayout

POINT = "implement.check.screenshots"
DECISION_POINT = "implement.check"
CATEGORY = "screenshots"
SCHEMA = Path(__file__).with_name("screenshots.schema.json")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {2: 3, 6: 4}  # 颜色类型 → 通道数：RGB、RGBA
VERDICTS = {"ok": Result.PASSED, "issue": Result.FAILED}
NO_SCREENSHOTS = "巡检没有产出受影响页面的截图"


@dataclass(frozen=True)
class CompareSettings:
    pixel_ratio: float  # 不同像素占比超过它才算有变化
    channel_tolerance: int  # 单个通道差值不超过它算相同(抗锯齿、字体渲染的细微差别)

    @classmethod
    def from_settings(cls, settings: Settings) -> CompareSettings:
        section = settings.section("implement.check.runtime")["screenshots"]
        return cls(float(section["pixelRatio"]), int(section["channelTolerance"]))


@dataclass
class ScreenshotReview:
    items: list[Item] = field(default_factory=list)
    awaiting: list[str] = field(default_factory=list)  # 等用户看的截图(key)：下次运行截图路径会变，按 key 认
    calls: int = 0
    tokens: Tokens = field(default_factory=Tokens)


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    channels: int
    pixels: bytes


def key(shot: Screenshot) -> str:
    """同一页面同一用例的截图跨运行稳定的名字：页面 + 用例目录名 + 文件名。"""
    path = Path(shot.path)
    return hashlib.sha256(f"{shot.page}\0{path.parent.name}\0{path.name}".encode()).hexdigest()[:24]


def baseline_dir(workspace: WorkspaceLayout, commit: str) -> Path:
    """基准 commit 的截图(store/files/layout 还没有这一项，先在这里算)。"""
    return workspace.cache_dir / "screenshots" / commit


def reviewed_dir(workspace: WorkspaceLayout, subject: str) -> Path:
    return workspace.cache_dir / "screenshots" / "reviewed" / subject


def changed(shots: Sequence[Screenshot], references: Sequence[Path], settings: CompareSettings) -> list[Screenshot]:
    """与所有参照目录中同名截图都不一致的。"""
    return [shot for shot in shots
            if not any(same(Path(shot.path), folder / f"{key(shot)}.png", settings) for folder in references)]


def same(left: Path, right: Path, settings: CompareSettings) -> bool:
    if not left.is_file() or not right.is_file():
        return False
    left_bytes, right_bytes = left.read_bytes(), right.read_bytes()
    if left_bytes == right_bytes:
        return True
    first, second = decode(left_bytes), decode(right_bytes)
    if first is None or second is None or (first.width, first.height, first.channels) != \
            (second.width, second.height, second.channels):
        return False
    return differing_ratio(first, second, settings.channel_tolerance) <= settings.pixel_ratio


def decode(data: bytes) -> Image | None:
    """8 位、RGB 或 RGBA、不隔行的 PNG；其他格式返回 None。"""
    if not data.startswith(PNG_SIGNATURE):
        return None
    offset = len(PNG_SIGNATURE)
    header: tuple[int, ...] | None = None
    chunks: list[bytes] = []
    while offset + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[offset:offset + 8])
        body = data[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            chunks.append(body)
        elif kind == b"IEND":
            break
    if header is None:
        return None
    width, height, depth, color, _, _, interlace = header
    if depth != 8 or color not in CHANNELS or interlace != 0:
        return None
    channels = CHANNELS[color]
    try:
        raw = zlib.decompress(b"".join(chunks))
    except zlib.error:
        return None
    pixels = _unfilter(raw, width * channels, height, channels)
    return None if pixels is None else Image(width, height, channels, pixels)


def differing_ratio(first: Image, second: Image, tolerance: int) -> float:
    if first.pixels == second.pixels:
        return 0.0
    channels = first.channels
    total = first.width * first.height
    different = 0
    a, b = first.pixels, second.pixels
    for start in range(0, len(a), channels):
        if a[start:start + channels] != b[start:start + channels] and any(
                abs(a[start + index] - b[start + index]) > tolerance for index in range(channels)):
            different += 1
    return different / total if total else 0.0


def review(runtime: Runtime, context: ImplementContext, shots: Sequence[Screenshot], *, raw_dir: Path,
           references: Sequence[Path], settings: CompareSettings) -> ScreenshotReview:
    if not shots:
        return ScreenshotReview([unverified("screenshots", CATEGORY, NO_SCREENSHOTS)])
    found = ScreenshotReview()
    to_review = changed(shots, references, settings)
    found.items += [Item(f"screenshot:{shot.path}", CATEGORY, Result.PASSED, None, (shot.path,),
                         "与基准或上一轮看过的截图一致，布局没变") for shot in shots if shot not in to_review]
    if not to_review:
        return found
    decided = user_verdict(context, to_review)
    if decided is not None:
        found.items += decided
        return found
    result = _call(runtime, context, to_review, raw_dir)
    found.calls = result.attempts
    found.tokens = result.tokens
    if result.status is not CallStatus.OK or result.output is None:
        reason = f"截图审查没有完成({result.status.value})，请用户查看"
        found.items += [Item(f"screenshot:{shot.path}", CATEGORY, Result.UNVERIFIED, None, (shot.path,), reason)
                        for shot in to_review]
        found.awaiting += [key(shot) for shot in to_review]
        return found
    by_path = {shot.path: shot for shot in to_review}
    answered = set()
    for entry in result.output["screenshots"]:
        shot = by_path.get(entry["path"])
        if shot is None:  # 模型写了不在清单中的路径：不认
            continue
        answered.add(shot.path)
        found.items.append(Item(f"screenshot:{shot.path}", CATEGORY, VERDICTS.get(entry["result"], Result.UNVERIFIED),
                                None, (shot.path,), entry["reason"]))
        if entry["result"] == "unknown":
            found.awaiting.append(key(shot))
    missing = [shot for shot in to_review if shot.path not in answered]
    found.items += [Item(f"screenshot:{shot.path}", CATEGORY, Result.UNVERIFIED, None, (shot.path,),
                         "截图审查漏看了这张，请用户查看") for shot in missing]
    found.awaiting += [key(shot) for shot in missing]
    return found


def user_verdict(context: ImplementContext, shots: Sequence[Screenshot]) -> list[Item] | None:
    """上一次自检停在等用户看截图上，之后用户给了结论：approve 为无问题，reject 的说明即问题。"""
    previous = context.last(DECISION_POINT)
    waiting = set((previous.facts.get("awaitingScreenshots") or []) if previous is not None else [])
    if not waiting or not {key(shot) for shot in shots} <= waiting:
        return None
    since = parse_iso(previous.created_at) if previous is not None and previous.created_at else None
    decision = next((item for item in reversed(context.decisions) if item.point == DECISION_POINT
                     and (since is None or parse_iso(item.at) >= since)), None)
    if decision is None:
        return None
    ok = decision.verdict == "approve"
    reason = "用户查看截图：无问题" if ok else f"用户查看截图：{decision.note}"
    return [Item(f"screenshot:{shot.path}", CATEGORY, Result.PASSED if ok else Result.FAILED, None, (shot.path,),
                 reason) for shot in shots]


def remember(shots: Sequence[Screenshot], items: Sequence[Item], folder: Path) -> None:
    """看过且无问题的截图留作下一轮的参照：下一轮像素没变就不再交给模型。"""
    passed = {path for item in items if item.result is Result.PASSED for path in item.evidence}
    folder.mkdir(parents=True, exist_ok=True)
    for shot in shots:
        if shot.path in passed and Path(shot.path).is_file():
            shutil.copyfile(shot.path, folder / f"{key(shot)}.png")


def _call(runtime: Runtime, context: ImplementContext, shots: Sequence[Screenshot], raw_dir: Path) -> CallResult:
    schema = load_schema(SCHEMA)
    model = runtime.settings.model_for(POINT)
    listing = "\n".join(f"- `{shot.path}`：受影响的页面 {shot.page}" for shot in shots)
    prompt = build(POINT, {"screenshots": listing}, language=runtime.language, schema=schema, tool=model.tool)
    params = params_for(POINT, settings=runtime.settings, run=runtime.run, subject=context.issue.id,
                        prompt=prompt.text, schema=schema, workdir=raw_dir, round=context.round,
                        read_paths=[Path(shot.path) for shot in shots], prompt_hash=prompt.hash)
    return call(params, runtime.agents)


def _unfilter(raw: bytes, stride: int, height: int, channels: int) -> bytes | None:
    """去掉每行的过滤(PNG 规范第 9 章：None、Sub、Up、Average、Paeth)。"""
    if len(raw) < (stride + 1) * height:
        return None
    output = bytearray(stride * height)
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for index in range(channels, stride):
                line[index] = (line[index] + line[index - channels]) & 0xFF
        elif kind == 2:
            line = bytearray((value + above) & 0xFF for value, above in zip(line, previous))
        elif kind == 3:
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif kind == 4:
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                upper_left = previous[index - channels] if index >= channels else 0
                line[index] = (line[index] + _paeth(left, previous[index], upper_left)) & 0xFF
        elif kind != 0:
            return None
        output[row * stride:(row + 1) * stride] = line
        previous = line
    return bytes(output)


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
    if distances[0] <= distances[1] and distances[0] <= distances[2]:
        return left
    return above if distances[1] <= distances[2] else upper_left
