"""extensions 各测试共用的环境：临时的本工具仓库(技术栈扩展)、工作区、用户目录，以及作为假扩展的 Python 脚本。

假扩展按请求的 options 行事：behavior 为 ok(输出 options.output)、error、garbage(输出不是 JSON)、crash(写标准错误后
以 3 退出)、flood(输出 options.bytes 个字节)、sleep(睡 options.seconds 秒，可先启动一个子进程并记下进程号)、
spec(把 options.document 写到 input.outputFile，按 spec-export 的格式输出，options.logFile 给出时写一行构建日志)；
extend 模式下项目扩展收到 base，按 extendBehavior(缺省 extend)在 base 的 options.key 列表后追加 options.append。
两层收到同样的 options：notes 取 options.notes，extend 时取 options.extendNotes。
options.record 给出时把收到的请求、工作目录与环境变量写入该文件；options.stderr 写入标准错误。

技术栈：stack(points) 为每个扩展点写一个名为 fake 的方法(清单的 command 缺省为 COMMAND)，并在 stack.yaml 的
defaults 中设为该扩展点的默认方法；method 另写一个方法。假扩展脚本写在技术栈目录(供直接构造 Implementation 的
测试)与每个方法目录下。
"""

import sys
from pathlib import Path

from tightrein.domain.enums import ExtensionPoint
from tightrein.store.files import yaml_text
from tightrein.store.files.layout import ToolLayout, UserLayout, WorkspaceLayout

PYTHON = sys.executable
STACK = "webstack"
METHOD = "fake"
SCRIPT = "fake_extension.py"
COMMAND = ["{python}", SCRIPT]

FAKE_EXTENSION = r'''import json
import os
import subprocess
import sys
import time

request = json.loads(sys.stdin.read())
options = request["options"]
if "record" in options:
    with open(options["record"], "w", encoding="utf-8") as handle:
        json.dump({"request": request, "cwd": os.getcwd(), "env": dict(os.environ)}, handle)
if "stderr" in options:
    sys.stderr.write(options["stderr"])
    sys.stderr.flush()
if request["base"] is None:
    behavior = options.get("behavior", "ok")
else:
    behavior = options.get("extendBehavior", "extend")
if behavior == "sleep":
    if "childPidFile" in options:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(options["childPidFile"], "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))
    time.sleep(options["seconds"])
if behavior == "garbage":
    print("not a json response")
    sys.exit(0)
if behavior == "crash":
    sys.stderr.write("".join(f"trace line {number}\n" for number in range(60)))
    sys.exit(3)
if behavior == "flood":
    sys.stdout.write("x" * options["bytes"])
    sys.exit(0)
response = {"protocol": options.get("protocol", request["protocol"])}
if behavior == "error":
    response["status"] = "error"
    response["error"] = {"code": options["code"], "message": options["message"], "hint": options.get("hint")}
elif behavior == "spec":
    with open(request["input"]["outputFile"], "w", encoding="utf-8") as handle:
        json.dump(options["document"], handle)
    if options.get("logFile"):
        with open(options["logFile"], "w", encoding="utf-8") as handle:
            handle.write("build succeeded\n")
    response["status"] = "ok"
    response["output"] = {"specFile": options.get("specFile", request["input"]["outputFile"]),
                          "format": "openapi-3.0", "operationCount": 1, "tool": {"name": "fake", "version": None},
                          "logFile": options.get("logFile")}
elif behavior == "extend":
    output = dict(request["base"])
    output[options["key"]] = output[options["key"]] + options["append"]
    response["status"] = "ok"
    response["output"] = output
else:
    response["status"] = "ok"
    response["output"] = options["output"]
notes_key = "notes" if request["base"] is None else "extendNotes"
if notes_key in options:
    response["notes"] = options[notes_key]
print(json.dumps(response, ensure_ascii=False))
sys.exit(options.get("exitCode", 0))
'''


class ExtensionWorld:
    def __init__(self, root: Path, make_config) -> None:
        self.root = root
        self.tool = ToolLayout(root / "tool")
        self.workspace = WorkspaceLayout(self.tool.workspaces_dir() / "demo")
        self.user = UserLayout(root / "home")
        self._make_config = make_config

    def stack(self, points, *, name=STACK, manifest_name=None, env=None, defaults=None) -> Path:
        """写入技术栈：stack.yaml 与每个扩展点的 fake 方法，返回技术栈目录。points 为扩展点到清单字段的映射。"""
        directory = self.tool.stack_dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SCRIPT).write_text(FAKE_EXTENSION, encoding="utf-8")
        for point, fields in points.items():
            self.method(point, METHOD, stack=name, **fields)
        manifest = {"name": manifest_name or name, "version": "1.0.0",
                    "defaults": {point: METHOD for point in points} if defaults is None else defaults}
        if env is not None:
            manifest["env"] = env
        self.tool.stack_manifest(name).write_text(yaml_text.dump(manifest), encoding="utf-8")
        return directory

    def method(self, point, method, *, stack=STACK, **fields) -> Path:
        """写入技术栈方法的清单与假扩展脚本，返回方法目录；fields 覆盖清单中的缺省字段(包括 name)。"""
        directory = self.tool.method_dir(stack, ExtensionPoint(point), method)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SCRIPT).write_text(FAKE_EXTENSION, encoding="utf-8")
        manifest = {"name": method, "point": point, "summary": "假方法", "applicability": "测试用",
                    "command": COMMAND, "optionsSchema": {"type": "object"}, **fields}
        self.tool.method_manifest(stack, ExtensionPoint(point), method).write_text(yaml_text.dump(manifest),
                                                                                   encoding="utf-8")
        return directory

    def project_extension(self) -> Path:
        """写入项目扩展的假脚本，返回工作区的 extensions/ 目录。"""
        directory = self.workspace.extensions_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SCRIPT).write_text(FAKE_EXTENSION, encoding="utf-8")
        return directory

    def config(self, **changes):
        return self._make_config(**changes)
