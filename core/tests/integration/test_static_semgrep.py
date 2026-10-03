"""static 的 Semgrep 集成测试：在临时仓库上以本地规则运行真实的 Semgrep；本机没有 semgrep 时跳过。"""

import os
import shutil

import pytest

from tightrein.domain.enums import ProbeLevel
from tightrein.sources.common.procs import SubprocessLauncher
from tightrein.sources.static.scope import Scope
from tightrein.sources.static.tools import semgrep

RULES = """rules:
  - id: empty-catch
    languages: [csharp]
    severity: WARNING
    message: 空的 catch 吞掉了异常
    pattern: |
      try { ... } catch ($E $X) { }
"""
SOURCE = """namespace Demo
{
    public class OrderService
    {
        public int Page(int size)
        {
            try
            {
                return 10 / size;
            }
            catch (System.DivideByZeroException error)
            {
            }
            return 0;
        }
    }
}
"""

pytestmark = pytest.mark.skipif(shutil.which("semgrep") is None, reason="本机没有 semgrep")


def test_real_semgrep_run(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "OrderService.cs").write_text(SOURCE, encoding="utf-8")
    rules = tmp_path / "rules.yaml"
    rules.write_text(RULES, encoding="utf-8")
    scope = Scope(ProbeLevel.INCREMENTAL, "a" * 40, "b" * 40, ("src/OrderService.cs",), ("src/OrderService.cs",))
    result = semgrep.run(SubprocessLauncher(), [str(rules)], scope, repo, tmp_path / "raw", dict(os.environ))
    assert result.status == "ok", result.notes
    assert [(item.file, item.line, item.severity) for item in result.findings] == [("src/OrderService.cs", 7, "medium")]
    assert result.findings[0].rule.endswith("empty-catch")
    assert (tmp_path / "raw" / "semgrep.json").is_file()
