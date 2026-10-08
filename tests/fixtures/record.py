"""重新录制整体测试用的模型输出：按 answers/ 跑一遍整体流程，把录制写进各情形的录制集(先清空)。

    .venv/bin/python tests/fixtures/record.py                         # 全部情形
    .venv/bin/python tests/fixtures/record.py --scenario regression   # 只录一种(fix、regression)
    .venv/bin/python tests/fixtures/record.py --dry-run               # 只试跑，不写录制集

answers/<调用点>.json 是该调用点的结构化结果(按该步骤的 schema)，<调用点>.patch 是可写调用(编码)在 worktree 中
产生的改动；同一调用点不同轮次的写成 <调用点>.r<轮>.json，同一(调用点、轮次)的第 N 次调用(如回归后又一次修复)
写成 <调用点>[.r<轮>].c<N>.json 与 .patch。regression 情形先找 answers/regression/，再找 answers/。
提示、schema 或流程变了导致回放报「replay-task-changed」或「replay-missing」时运行本脚本；缺答案的调用点会列出来。
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures import sample


def record(name: str, scratch: Path, dry_run: bool) -> None:
    scenario = sample.SCENARIOS[name]
    root = scratch / name
    h = sample.harness(root, record=True, scenario=name)
    results = sample.drive(h)
    print(f"== 情形 {name}")
    for index, result in enumerate(results, 1):
        print(f"第 {index} 次运行：退出码 {result.code}")
        for step in result.data.get("result", {}).get("steps", []):
            print(f"  {step['stage']}  {step['subject']}  {step['status']}  {step['summary'][:300]}")
    print("Issue：", sample.issue_statuses(h.sample))
    recorder = h.replay
    assert isinstance(recorder, sample.Recorder)
    if recorder.missing:
        print("缺答案的调用：\n" + "\n".join(f"  {item}" for item in recorder.missing))
    if h.github.unknown:
        print("假 GitHub 不认识的命令：", h.github.unknown)
    if dry_run:
        return
    shutil.rmtree(scenario.recordings, ignore_errors=True)
    shutil.copytree(root / "recordings", scenario.recordings)
    print(f"已写入 {scenario.recordings}：{len(recorder.entries)} 次调用")


def main() -> int:
    names = list(sample.SCENARIOS)
    if "--scenario" in sys.argv:
        names = [sys.argv[sys.argv.index("--scenario") + 1]]
    with tempfile.TemporaryDirectory(prefix="tightrein-record-") as scratch, sample.offline():
        for name in names:
            record(name, Path(scratch), "--dry-run" in sys.argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
