"""以 replay 执行器运行真实 Runner 的测试辅助：本工具仓库初始化为 git 仓库，录制执行器结果，构造 Runner。

retrieval 的去重判断与 evaluation 的模型评审都经执行器运行；测试中不调用任何模型，按 (角色, 对象编号, attempt) 与
任务哈希回放录制的结构化结果，guards 的运行前后检查照常执行。
"""

import json

from tightrein.config.user import UserConfig
from tightrein.guards.policy import GuardSettings
from tightrein.guards.service import Guards
from tightrein.runner.adapters.replay import ReplayAdapter
from tightrein.runner.recording import RecordingSet, index_entry
from tightrein.runner.registry import Registry, default_adapters
from tightrein.runner.service import Runner
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.process import VcsProcess


def init_tool_repo(repos, root):
    """本工具仓库：工作区的 data/ 不纳入版本管理。"""
    root.mkdir(parents=True, exist_ok=True)
    repos.git(root, "init", "-q", "-b", "main")
    repos.write(root, ".gitignore", "workspaces/*/data/\n")
    repos.commit(root, "chore: init", {"README.md": "# tightrein\n"})


def runner_result(output, status="ok", error_type=None, cost=0.01):
    return {"status": status, "errorType": error_type, "output": output,
            "usage": {"inputTokens": 800, "outputTokens": 120, "cachedInputTokens": None, "costUsd": cost,
                      "costEstimated": False},
            "durationMs": 1000, "attempts": 1, "tool": "claude", "model": "claude-opus", "sessionId": "s-1",
            "transcriptPath": None, "guardReport": None, "violations": []}


def record(root, task, output, status="ok", error_type=None):
    """录制一次执行器调用：回放时按 (角色, 对象编号, attempt) 与任务哈希取出。"""
    directory = root / f"{task.role}-{task.subject_id}.{task.attempt}"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps(runner_result(output, status, error_type)), encoding="utf-8")
    index = root / "index.json"
    entries = json.loads(index.read_text(encoding="utf-8"))["recordings"] if index.exists() else []
    entries.append(index_entry(task, directory.name))
    index.write_text(json.dumps({"recordings": entries}), encoding="utf-8")


def replay_runner(*, conn, layout, tool, config, tracer, redactor, environ, recordings):
    process = VcsProcess(environ=environ)
    guards = Guards(GitReader(process), GuardSettings(), layout, tool, tracer=tracer)
    registry = Registry(default_adapters(tool.root / "home"), UserConfig(tool.root / "config.yaml"),
                        which=lambda name: None)
    return Runner(conn=conn, layout=layout, tool_layout=tool, config=config, registry=registry, guards=guards,
                  launcher=None, tracer=tracer, redactor=redactor, environ=environ,
                  replay=ReplayAdapter(RecordingSet(recordings), process), monotonic=lambda: 0.0)
