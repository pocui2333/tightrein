"""packaging 测试共用：临时的工具根目录与家目录、内存中构造的源码归档、记录调用的外部命令替身。

不访问网络、不调用真实的 claude、gh、launchctl，不写真实的家目录。
"""

import io
import json
import tarfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from tightrein.cli.main import build_parser
from tightrein.config import project, user
from tightrein.domain.clock import FixedClock
from tightrein.packaging import claude, install, skills_check, third_party
from tightrein.pipeline.learn.steps.third_party import RepoFacts
from tightrein.sources.common.http import HttpResponse
from tightrein.store.files.layout import ToolLayout
from tightrein.vcs.process import Completed, VcsProcess

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
SOURCE = "https://github.com/example/skills"
NOW = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)
FACTS = RepoFacts(7296, date(2026, 9, 28), False, "CC-BY-SA-4.0")
REVIEW_DIR = "plugins/review/skills/differential-review"
VARIANT_DIR = "plugins/variant/skills/variant-analysis"


def skill_text(name, body="用于测试。\n"):
    return f"---\nname: {name}\ndescription: 测试用的 {name}\n---\n\n# {name}\n\n{body}"


def archive(files, top="skills-main"):
    """tar.gz：顶层为一个目录，files 为 {相对路径: 文本}。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def upstream(review_body="审查差异。\n"):
    return archive({
        f"{REVIEW_DIR}/SKILL.md": skill_text("differential-review", review_body),
        f"{REVIEW_DIR}/references/methodology.md": "方法\n",
        f"{VARIANT_DIR}/SKILL.md": skill_text("variant-analysis"),
        "README.md": "仓库说明\n",
    })


class Downloads:
    """按 commit 返回归档的传输替身。"""

    def __init__(self, archives=None):
        self.archives = archives or {COMMIT_A: upstream(), COMMIT_B: upstream("改过的正文。\n")}
        self.urls = []

    def __call__(self, request):
        self.urls.append(request.url)
        commit = request.url.rsplit("/", 1)[-1]
        if commit not in self.archives:
            return HttpResponse(404, b"")
        return HttpResponse(200, self.archives[commit])


class Commands:
    """外部命令的替身：记录参数；gh api 与 claude plugin list 按参数应答，其余成功。"""

    def __init__(self, failing=()):
        self.calls = []
        self.failing = set(failing)
        self.marketplace = None

    def __call__(self, command):
        argv = command.argv
        self.calls.append(argv)
        if argv[1:2] and argv[1] in self.failing:
            return Completed(argv, 5, "", "失败的原因")
        if argv[:2] == ("gh", "api") and argv[2].endswith("/commits/HEAD"):
            return Completed(argv, 0, json.dumps({"sha": COMMIT_A}))
        if argv[:2] == ("gh", "api"):
            return Completed(argv, 0, json.dumps({"stargazers_count": FACTS.stars, "pushed_at": "2026-09-28T01:00:00Z",
                                                  "archived": False, "license": {"spdx_id": FACTS.license}}))
        if argv[1:4] == ("plugin", "marketplace", "add"):
            self.marketplace = Path(argv[4])
        if argv[1:3] == ("plugin", "list"):
            manifest = None if self.marketplace is None else (
                self.marketplace / claude.PLUGIN / claude.MANIFEST_DIR / "plugin.json")
            if manifest is None or not manifest.is_file():
                return Completed(argv, 0, "[]")
            version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
            return Completed(argv, 0, json.dumps([{"id": claude.PLUGIN_ID, "version": version}]))
        return Completed(argv, 0, "")

    def named(self, program):
        return [list(argv) for argv in self.calls if argv[0] == program]


LOCK = ("lockVersion: 1\nskills:\n"
        f"  - name: differential-review\n    source: {SOURCE}\n"
        f"  - name: variant-analysis\n    source: {SOURCE}\n")


def make_tool(root):
    """工具根目录：两个本工具的 skill 与只有 name、source 的锁定清单。"""
    (root / "skills" / "loop").mkdir(parents=True)
    (root / "skills" / "loop" / "SKILL.md").write_text(skill_text("loop", "执行 `tightrein status --json`。\n"),
                                                        encoding="utf-8")
    (root / "skills" / "fix" / "references").mkdir(parents=True)
    (root / "skills" / "fix" / "SKILL.md").write_text(skill_text("fix", "见 `references/rules.md`。\n"),
                                                       encoding="utf-8")
    (root / "skills" / "fix" / "references" / "rules.md").write_text("规则\n", encoding="utf-8")
    (root / "third_party").mkdir()
    (root / "third_party" / "skills.lock.yaml").write_text(LOCK, encoding="utf-8")
    return root


@dataclass
class PackagingWorld:
    tool_root: Path
    home: Path
    downloads: Downloads = field(default_factory=Downloads)
    commands: Commands = field(default_factory=Commands)

    @property
    def tool(self):
        return ToolLayout(self.tool_root)

    def context(self):
        config = project.core_config()
        process = VcsProcess(execute=self.commands, environ={"PATH": "/usr/bin:/bin"}, sleep=lambda seconds: None)
        return install.Context(self.tool, self.home, user.load(home=self.home), config, FixedClock(NOW), timezone.utc,
                               process, third_party.fetcher(self.downloads, 1.0),
                               skills_check.command_tree(build_parser()))

    def write_user_config(self, text):
        path = user.default_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def lock_all(self):
        """锁定清单中的全部条目到 COMMIT_A 并写回。"""
        ctx = self.context()
        locked, _ = third_party.lock(ctx.lock(), set(), ref=None, head=lambda source: COMMIT_A,
                                     facts=lambda source: FACTS, fetch=ctx.fetch, cache=ctx.cache, config=ctx.config,
                                     clock=ctx.clock)
        third_party.write_lock(self.tool.third_party_lock(), locked)
        return locked


def make_world(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return PackagingWorld(make_tool(tmp_path / "tool"), home)
