"""生产发布记录(architecture/07 19.8)：合并提交已进入 origin/master 时记录日期。"""

from __future__ import annotations

from collections.abc import Sequence

MASTER = "origin/master"


def in_master(branches: Sequence[str]) -> bool:
    return MASTER in branches
