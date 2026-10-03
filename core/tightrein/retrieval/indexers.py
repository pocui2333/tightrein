"""索引器(architecture/03 1.4、1.10)：同步对每个变化的行依次调用全部索引器。

FtsIndexer 把标题、摘要、标签与正文经 split_cjk 处理后写入 knowledge_fts。引入向量检索时新增 VectorIndexer
实现同一接口，sync 不需要改动。
"""

from __future__ import annotations

import sqlite3
from typing import Protocol

from tightrein.retrieval.frontmatter import IndexedEntry
from tightrein.retrieval.text import split_cjk
from tightrein.store.repos import knowledge


class Indexer(Protocol):
    def upsert(self, entry: IndexedEntry, conn: sqlite3.Connection) -> None: ...

    def remove(self, entry_id: str, conn: sqlite3.Connection) -> None: ...


class FtsIndexer:
    def upsert(self, entry: IndexedEntry, conn: sqlite3.Connection) -> None:
        knowledge.save_text(conn, entry.id, split_cjk(entry.title), split_cjk(entry.summary),
                            split_cjk(" ".join(entry.tags)), split_cjk(entry.body))

    def remove(self, entry_id: str, conn: sqlite3.Connection) -> None:
        knowledge.remove_text(conn, entry_id)
