"""编排测试共用：记录调用的模块替身。每个模块的方法返回预置结果或缺省的空结果，可以注入会改动数据库的动作。"""

from types import SimpleNamespace

from tightrein.domain.enums import Probe
from tightrein.runner.roles import Overrides
from tightrein.store.retention import RetentionReport

# 缺省全部未启用：sources 步骤不运行采集，只在摘要中列出
ALL_DISABLED = {probe.value: "未配置" for probe in (Probe.PLATFORM_ERRORS, Probe.ACCESS_LOG, Probe.ALERTS,
                                                   Probe.PROJECT_PROBE)}
DEFAULTS = {
    "collect.deployments": None,
    "collect.run": SimpleNamespace(run=None),
    "aggregate.run": SimpleNamespace(status=None, message="没有新信号"),
    "triage.run": SimpleNamespace(items=[], skipped=[], message="没有问题", summary="", blocked=None),
    "issue.create": SimpleNamespace(items=[], skipped=[]),
    "release.track": SimpleNamespace(lines=[]),
    "learn.lessons": SimpleNamespace(outputs={}),
    "learn.report": SimpleNamespace(outputs={}),
    "learn.health": SimpleNamespace(outputs={"health": []}),
}


class Module:
    def __init__(self, owner, name):
        self.owner = owner
        self.name = name

    def __getattr__(self, method):
        key = f"{self.name}.{method}"

        def call(*args, **kwargs):
            self.owner.calls.append((key, args))
            action = self.owner.actions.get(key)
            if isinstance(action, BaseException):
                raise action
            if callable(action):
                return action(*args, **kwargs)
            return action if key in self.owner.actions else DEFAULTS.get(key)

        return call


class FakeModules:
    def __init__(self, world, deploy_source=True, disabled=None, **actions):
        self.world = world
        self.deploy_source = deploy_source
        self.disabled = dict(ALL_DISABLED if disabled is None else disabled)
        self.actions = {key.replace("_", "."): value for key, value in actions.items()}
        self.calls = []
        self.commands = []
        self.waits = []
        self.overrides = Overrides()

    def names(self):
        return [key for key, _ in self.calls]

    def collect(self):
        return Module(self, "collect")

    def aggregate(self):
        return Module(self, "aggregate")

    def triage(self):
        return Module(self, "triage")

    def issue(self):
        return Module(self, "issue")

    def fix(self):
        return Module(self, "fix")

    def verify(self):
        return Module(self, "verify")

    def release(self):
        return Module(self, "release")

    def learn(self):
        return Module(self, "learn")

    def main_head(self):
        self.calls.append(("main_head", ()))
        return self.actions.get("main.head")

    def sync_readonly(self, commit=None):
        self.calls.append(("sync_readonly", (commit,)))
        return commit

    def deploys(self):
        """部署来源的替身：deploy_source 为假时视为没有配置部署来源。"""
        return SimpleNamespace(configured=lambda: self.deploy_source)

    def purge(self):
        self.calls.append(("purge", ()))
        return RetentionReport(0, (), (), ())

    def disabled_sources(self):
        return dict(self.disabled)

    def wait_until(self, moment):
        self.waits.append(moment)
        self.world.clock.advance(moment - self.world.clock.now())

    def run_command(self, argv):
        self.commands.append(list(argv))
        action = self.actions.get("run.command")
        return action(argv) if callable(action) else 0
