"""已接受的取舍(architecture/06 4.9)：取证给出 tradeoffHit 时由代码核对编号在知识库中存在、类型为已接受的取舍、
状态为 active，通过才判为已接受的取舍。"""

from __future__ import annotations

import sqlite3

from tightrein.domain.enums import KnowledgeStatus, KnowledgeType
from tightrein.store.repos import knowledge


def tradeoff_valid(conn: sqlite3.Connection, knowledge_id: str | None) -> bool:
    if knowledge_id is None:
        return False
    record = knowledge.get(conn, knowledge_id)
    return record is not None and record.type == KnowledgeType.TRADEOFF.value \
        and record.status is KnowledgeStatus.ACTIVE
