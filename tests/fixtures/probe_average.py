"""示例项目的探针(方法 02「检查业务状态」的最小写法)：取远程主分支(示例项目的「线上版本」)的 calc.py，
真跑 average([])。

崩溃即报一条信号；修好并合并后不再报(验收按「覆盖而没再出现」确认)。只读：用 `git archive --remote` 取文件内容，
不动仓库的工作目录与引用。
"""

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

from tightrein.collect.project_probes import helpers

FINGERPRINT = "average-empty:calc.average"


def main() -> None:
    found = helpers.read_input()
    settings = json.loads((Path(found["workspace"]) / "settings.json").read_text(encoding="utf-8"))
    repo = settings["project"]["repo"]
    archive = subprocess.run(["git", "-C", repo, "archive", "--remote=origin", "main", "calc.py"],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        member = bundle.extractfile("calc.py")
        assert member is not None
        source = member.read().decode("utf-8")
    namespace: dict[str, object] = {}
    exec(compile(source, "calc.py", "exec"), namespace)  # noqa: S102 示例探针：执行被测项目自己的代码
    try:
        namespace["average"]([])  # type: ignore[operator]
    except ZeroDivisionError as error:
        evidence = f"average([]) → {type(error).__name__}: {error}"
        signal = helpers.signal("calc.py:average", "average([]) 对空列表抛出 ZeroDivisionError", [evidence],
                                FINGERPRINT, severity_hint="P2", context={"input": []})
        helpers.emit([signal], state={"checked": "origin main"})
        return
    helpers.emit([], state={"checked": "origin main"}, notes=["average([]) 不再崩溃"])


if __name__ == "__main__":
    main()
    sys.exit(0)
