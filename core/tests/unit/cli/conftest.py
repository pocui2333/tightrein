"""复用 pipeline 与 packaging 测试的样例数据与替身(pipeline_world、triage_world、packaging_world 等)。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "pipeline"))
sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
