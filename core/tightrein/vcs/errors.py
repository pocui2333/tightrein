"""vcs 的错误类型(architecture/02 4.8)。

每个错误保留原始命令、退出码与错误输出的末尾(已脱敏)；由底层异常引起的以 `raise ... from` 保留原异常。
分类只依据退出码与 porcelain 输出，不解析错误消息文字。
"""

from __future__ import annotations

from collections.abc import Sequence


class VcsError(Exception):
    def __init__(
        self, message: str, *, argv: Sequence[str] = (), returncode: int | None = None, stderr: str = ""
    ) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(message)


class ProgramNotFound(VcsError):
    """可执行文件不存在。"""


class GitNotFound(ProgramNotFound):
    pass


class GhNotFound(ProgramNotFound):
    pass


class GhAuthError(VcsError):
    """gh 未登录或权限不足(gh 退出码 4)，提示用户执行 gh auth login。"""


class NetworkError(VcsError):
    """访问远程失败：fetch、push 以退出码 128 结束，或访问远程的命令超时。只读查询重试 2 次。"""


class RefNotFound(VcsError):
    """commit 或分支不存在。"""


class PushRejected(VcsError):
    """远程拒绝推送；rejected 为被拒绝的引用。"""

    def __init__(
        self, message: str, *, rejected: Sequence[str], argv: Sequence[str] = (), returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        self.rejected = tuple(rejected)
        super().__init__(message, argv=argv, returncode=returncode, stderr=stderr)


class MergeConflict(VcsError):
    """合并出现冲突；files 为冲突文件，按文件名排序。"""

    def __init__(
        self, message: str, *, files: Sequence[str], argv: Sequence[str] = (), returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        self.files = tuple(files)
        super().__init__(message, argv=argv, returncode=returncode, stderr=stderr)


class WorktreeDirty(VcsError):
    """worktree 中有未提交的改动；paths 为改动的文件。"""

    def __init__(self, message: str, *, paths: Sequence[str] = ()) -> None:
        self.paths = tuple(paths)
        super().__init__(message)


class WorktreeLocked(VcsError):
    """只读 worktree 上有 guards 的锁定标记，须先由 guards.recover 恢复。"""


class CommandFailed(VcsError):
    """git 或 gh 的其他非零退出。"""


class GitCommandError(CommandFailed):
    pass


class GhCommandError(CommandFailed):
    pass
