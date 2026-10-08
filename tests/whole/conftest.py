"""整体测试共用：把 tests/ 放进导入路径，按包名 fixtures 导入共用样例(importlib 模式不改 sys.path)。"""

import sys
from pathlib import Path

TESTS = str(Path(__file__).resolve().parents[1])
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
