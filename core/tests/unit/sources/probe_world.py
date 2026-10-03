"""probes 各测试共用的基础：固定时钟、可复现的随机源、目标与已迁移的数据库。"""

from datetime import datetime, timezone

from tightrein.domain.clock import FixedClock
from tightrein.observability.redact import Redactor
from tightrein.sources.base import ProbeTarget
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.store.migrations.runner import open_database

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
RELEASE = "d6f37025a1b2c3d4e5f60718293a4b5c6d7e8f90"


class CountingRandom:
    """每次返回递增的字节串，信号编号因此可以复现且互不相同。"""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, size: int) -> bytes:
        self.count += 1
        return self.count.to_bytes(size, "big")


def make_target(tmp_path, probe="api-fuzz", **changes):
    values = {
        "environment": "staging",
        "run_id": f"R-20261005-030000-collect-{probe}",
        "raw_dir": tmp_path / "raw" / probe,
        "clock": FixedClock(NOW),
        "base_url": "https://staging.example.test",
        "release": RELEASE,
    }
    values.update(changes)
    return ProbeTarget(**values)


def make_redactor(*secrets):
    redactor = Redactor()
    for secret in secrets:
        redactor.register(secret)
    return ProbeRedactor(redactor)


def open_db(tmp_path):
    return open_database(tmp_path / "data" / "tightrein.db", FixedClock(NOW))
