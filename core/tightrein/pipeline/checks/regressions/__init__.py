"""复现检查(architecture/04 第 7 节)：清单的读取与校验、接口、页面、静态三类检查的执行。"""

from tightrein.domain.enums import RegressionKind

WORKTREE_KINDS = (RegressionKind.STATIC, RegressionKind.TEST)  # 在 worktree 上执行、不需要服务的复现检查
SERVICE_KINDS = (RegressionKind.API, RegressionKind.PAGE)  # 需要本机服务或目标环境的复现检查
