"""冒烟测试的演示工作区：复制 tests/fixtures/demo_workspace 到临时目录，改写仓库与归档目录的路径，建立只读
worktree 的源文件；工具根目录为只含 skills/ 的副本(边界检查只扫描它)；git 与 gh 为按参数应答的替身，信号编号的
随机数固定，不启动任何外部进程。

回放录制集：夹具中只有各角色的 result.json(录制下来的结构化结果)。任务哈希覆盖提示正文，而提示中含有临时目录的
路径，因此按录制的做法补 index.json：在同一个临时目录中运行一次，读取执行器留下的 task.json(其中有任务哈希)，
把缺少录制的角色登记进 index.json，重建工作区后再运行，直到没有缺少录制的角色。
"""

import io
import json
import shutil
from pathlib import Path

from cli_world import FakeVcs, Notices
from pipeline_world import CountingRandom

from tightrein.cli.assemble import Externals
from tightrein.cli.main import main
from tightrein.runner.result import REPLAY_MISSING, REPLAY_TASK_CHANGED
from tightrein.store.files import yaml_text
from tightrein.store.files.layout import WorkspaceLayout

FIXTURE = Path(__file__).parents[2] / "fixtures" / "demo_workspace"
SKILLS = Path(__file__).parents[4] / "skills"
COMMIT = "c" * 40
NOW = "2026-10-05T09:00:00+09:00"
MAX_RECORDING_PASSES = 5


class DemoWorld:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "workspace"
        self.archive = tmp_path / "archive"
        self.repo = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.replay = tmp_path / "replay"
        self.tool = tmp_path / "tool"
        shutil.copytree(SKILLS, self.tool / "skills")
        (self.tool / "extensions" / "stacks").mkdir(parents=True)
        self.reset()

    def reset(self) -> None:
        """从夹具重建工作区；临时目录的路径不变，同样的命令产生同样的执行器任务。"""
        for path in (self.root, self.archive, self.repo, self.home):
            if path.exists():
                shutil.rmtree(path)
        shutil.copytree(FIXTURE, self.root, ignore=shutil.ignore_patterns("replay", "source", "archive"))
        shutil.copytree(FIXTURE / "archive", self.archive)
        shutil.copytree(FIXTURE / "source", WorkspaceLayout(self.root).readonly_worktree())
        self.repo.mkdir()
        self.home.mkdir()
        path = self.root / "project.yaml"
        data = yaml_text.load(path.read_text(encoding="utf-8"))
        data["project"]["repo"] = str(self.repo)
        task = data["schedule"]["tasks"][0]
        task["command"] = task["command"].replace("ARCHIVE", str(self.archive))
        path.write_text(yaml_text.dump(data), encoding="utf-8")
        self.vcs = FakeVcs((("run", "list"), "[]"), (("rev-parse",), COMMIT + "\n"), (("pr", "list"), "[]"))
        self.notices = Notices()
        self.random = CountingRandom()

    def externals(self) -> Externals:
        return Externals(environ={"PATH": "/usr/bin:/bin"}, home=self.home, tool_root=self.tool, vcs_execute=self.vcs,
                         notify_run=self.notices, which=lambda name: None, stdin_is_tty=lambda: False,
                         sleep=lambda seconds: None, randomness=self.random)

    def call(self, *argv: str, now: str = NOW) -> tuple[int, dict]:
        out = io.StringIO()
        code = main([*argv, "--workspace", str(self.root), "--json", "--now", now], self.externals(),
                    stdin=io.StringIO(), stdout=out, stderr=io.StringIO())
        return code, json.loads(out.getvalue())

    def layout(self) -> WorkspaceLayout:
        return WorkspaceLayout(self.root)

    # 录制集

    def _missing(self) -> list[dict]:
        found = []
        for task_file in sorted(self.layout().runs_dir().rglob("task.json")):
            result = json.loads((task_file.parent / "result.json").read_text(encoding="utf-8"))
            if result["errorType"] in (REPLAY_MISSING, REPLAY_TASK_CHANGED):
                found.append(json.loads(task_file.read_text(encoding="utf-8")))
        return found

    def replayed(self, steps: list[tuple[tuple[str, ...], str]]) -> list[tuple[int, dict]]:
        """依次执行 (参数, --now)，全部使用回放录制集。"""
        return [self.call(*argv, "--runner", "replay", "--replay-from", str(self.replay), now=now)
                for argv, now in steps]

    def record(self, steps: list[tuple[tuple[str, ...], str]]) -> Path:
        """按 steps 反复运行，补齐录制集的 index.json；结束后重建工作区，返回录制集目录。"""
        if self.replay.exists():
            shutil.rmtree(self.replay)
        shutil.copytree(FIXTURE / "replay", self.replay)
        entries = [{"role": "none", "subjectId": "none", "attempt": 1, "dir": "none", "taskSha256": "0" * 64}]
        for _ in range(MAX_RECORDING_PASSES):
            (self.replay / "index.json").write_text(json.dumps({"recordings": entries}), encoding="utf-8")
            self.replayed(steps)
            missing = self._missing()
            self.reset()
            if not missing:
                return self.replay
            for task in missing:
                directory = next(item.name for item in (FIXTURE / "replay").iterdir()
                                 if task["role"].startswith(item.name))
                entries.append({"role": task["role"], "subjectId": task["subject"]["id"],
                                "attempt": task["attempt"], "dir": directory, "taskSha256": task["taskSha256"]})
        raise AssertionError(f"{MAX_RECORDING_PASSES} 次运行后仍有缺少录制的角色")
