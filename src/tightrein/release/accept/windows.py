"""各来源的观察期：部署之后要等多久没再出现，才算问题真的没了。

- 运行时才会出现的来源(平台报错、访问日志、业务告警、项目探针、API 模糊测试)：观察期内再出现即回归，期满没再出现为通过；
- 确定性来源(静态巡检、任务外发现)：问题在代码里，合并部署后即可确认，观察期为 0；
- 没有关联问题的 Issue(用户提的需求)：没有「线上再出现」可看，部署后即确认；
- 取值在 settings 的 controls."release.accept".windows：键为来源的控制键，`default` 为没列出的来源。
"""

from __future__ import annotations

from datetime import timedelta

from tightrein.protocol.naming import parse_duration
from tightrein.settings.load import Settings

POINT = "release.accept"
DEFAULT = "default"


def window(source: str, settings: Settings) -> timedelta:
    windows = settings.control(POINT, "windows")
    return timedelta(seconds=parse_duration(str(windows.get(source, windows[DEFAULT]))))
