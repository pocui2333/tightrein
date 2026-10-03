"""cli 测试共用：临时工作区与 project.yaml、按参数应答的假 git/gh、假传输与记录调用的通知命令。"""

import subprocess
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path

from pipeline_world import PROJECT

from tightrein.cli.assemble import App, Externals, Options
from tightrein.sources.common.http import HttpResponse
from tightrein.store.files import yaml_text
from tightrein.vcs.process import Completed

ZONE = timezone(timedelta(hours=9))
NOW = "2026-10-05T12:00:00+09:00"
COMMIT = "c" * 40


class FakeVcs:
    """git 与 gh 的替身：routes 为 (参数片段, 标准输出或 (退出码, 标准输出))，按顺序取第一个全部片段都出现的；
    没有匹配时 git 返回空输出成功，gh 抛出 AssertionError。"""

    def __init__(self, *routes):
        self.routes = list(routes)
        self.commands = []

    def add(self, fragment, outcome):
        self.routes.insert(0, (tuple(fragment), outcome))
        return self

    def __call__(self, command):
        self.commands.append(command.argv)
        for fragment, outcome in self.routes:
            if all(part in command.argv for part in fragment):
                code, stdout = outcome if isinstance(outcome, tuple) else (0, outcome)
                return Completed(command.argv, code, stdout, "" if code == 0 else "failed")
        if command.argv[0] == "gh":
            raise AssertionError(f"没有模拟的 gh 命令：{command.argv}")
        return Completed(command.argv, 0, "")


class FakeTransport:
    def __init__(self, status=200):
        self.status = status
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return HttpResponse(self.status, b"{}")


class Notices:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 0, "", "")


@dataclass
class CliWorld:
    root: Path
    home: Path
    vcs: FakeVcs = field(default_factory=FakeVcs)
    transport: FakeTransport = field(default_factory=FakeTransport)
    notices: Notices = field(default_factory=Notices)
    tty: bool = False
    extension_runner: object = None

    def externals(self, **changes):
        values = dict(environ={"PATH": "/usr/bin:/bin"}, home=self.home, zone=ZONE, vcs_execute=self.vcs,
                      transport=self.transport, notify_run=self.notices, which=lambda name: None,
                      stdin_is_tty=lambda: self.tty, sleep=lambda seconds: None,
                      extension_runner=self.extension_runner)
        values.update(changes)
        return Externals(**values)

    def app(self, **options):
        values = dict(workspace=self.root, now=NOW)
        values.update(options)
        return App(Options(**values), self.externals())


def make_cli_world(tmp_path, **config_changes):
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    data = {**PROJECT, "project": {**PROJECT["project"], "repo": str(tmp_path / "repo")}, **config_changes}
    (root / "project.yaml").write_text(yaml_text.dump(data), encoding="utf-8")
    (tmp_path / "repo").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return CliWorld(root, home)
