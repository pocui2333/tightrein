"""core/semgrep 的集成测试：经方法的运行框架以本地规则运行真实的 Semgrep；本机没有 semgrep 时跳过。"""

import os
import shutil

import pytest

from tightrein.config.layers import core_defaults
from tightrein.extensions.catalog import read_core
from tightrein.extensions.methods.runtime import MethodContext, respond
from tightrein.extensions.methods.static_tools import semgrep

RULES = """rules:
  - id: bare-except-pass
    languages: [python]
    severity: WARNING
    message: 异常被吞掉
    pattern: |
      try:
          ...
      except:
          pass
"""
SOURCE = """def page(size):
    try:
        return 10 / size
    except:
        pass
    return 0
"""

pytestmark = pytest.mark.skipif(shutil.which("semgrep") is None, reason="本机没有 semgrep")


def test_real_semgrep_through_the_method(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "paging.py").write_text(SOURCE, encoding="utf-8")
    rules = tmp_path / "rules.yaml"
    rules.write_text(RULES, encoding="utf-8")
    document = {
        "protocol": 1, "point": "static-tools", "workspace": str(tmp_path), "repo": str(repo), "commit": "d6f37025",
        "options": {**core_defaults()["methods"]["core/semgrep"], "configs": [str(rules)]},
        "scratchDir": str(tmp_path / "scratch"), "base": None,
        "input": {"level": "incremental", "baseCommit": "a1b2c3d", "changedFiles": ["src/paging.py"],
                  "rawDir": str(tmp_path / "raw")},
    }
    response = respond(document, semgrep.run, read_core(semgrep.MANIFEST), MethodContext(environ=dict(os.environ)))
    assert response["status"] == "ok"
    assert response["output"]["tools"][0]["status"] == "ok", response["output"]["tools"]
    findings = response["output"]["findings"]
    assert [(item["file"], item["line"], item["severity"]) for item in findings] == [("src/paging.py", 2, "medium")]
    assert findings[0]["rule"].endswith("bare-except-pass")
    assert (tmp_path / "raw" / "core-semgrep" / "semgrep.json").is_file()
