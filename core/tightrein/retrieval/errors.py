"""retrieval 的异常类型(architecture/03 1.11)。

命令行按类型给出退出码，MCP 服务按类型区分可以修正参数重试的错误与整个调用失败。
"""

from __future__ import annotations

from dataclasses import dataclass


class KnowledgeError(Exception):
    """retrieval 全部异常的基类。"""


class InvalidQuery(KnowledgeError):
    """查询串处理后为空、过滤条件取值不合法、limit 越界。"""


class EntryNotFound(KnowledgeError):
    """编号在索引中不存在，或文件已被删除。"""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        super().__init__(f"没有编号为 {entry_id} 的条目；可以先用 kb search <关键词> 查找编号")


class IndexUnavailable(KnowledgeError):
    """数据库文件不存在或迁移未执行。"""


class LockTimeout(KnowledgeError):
    """对象锁 knowledge 被其他进程持有，等待超时。"""


@dataclass(frozen=True, order=True)
class FrontmatterIssue:
    """同步中发现的一个问题：文件(相对工作区)、frontmatter 内的 JSON 路径与原因。"""

    path: str
    pointer: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path} {self.pointer}: {self.reason}"


class SyncFailed(KnowledgeError):
    """同步发现格式或一致性错误，本次没有写入任何变化。"""

    def __init__(self, issues: tuple[FrontmatterIssue, ...]) -> None:
        self.issues = issues
        lines = "\n".join(str(issue) for issue in issues)
        super().__init__(f"知识文件有 {len(issues)} 处错误，索引保持上一次成功同步后的状态：\n{lines}")


class DraftInvalid(KnowledgeError):
    """写入的草稿不合格：字段缺失、简称或标签格式错误、related 中的编号不存在。"""


class BaselineMismatch(KnowledgeError):
    """检索评测的基线不存在，或与本次使用的用例集不同，不能比较。"""


class WriteDecisionInvalid(KnowledgeError):
    """去重判断在重试一次后仍不合格，草稿没有写入。"""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__("去重判断不合格，草稿没有写入：" + "；".join(reasons))
