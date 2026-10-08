"""取法一：按查询条件从日志平台取访问日志原文。日志平台方法与平台错误共用(platform_errors/log_platform/)，
参数取 controls."collect.access_log".<方法名>，地址取 sites.json，凭据取 secrets.json。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from tightrein.collect.common.source import SourceMisconfigured
from tightrein.collect.platform_errors.log_platform.chunks import LogRead
from tightrein.protocol import methods
from tightrein.protocol.http import Transport

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

LOG_METHODS = "tightrein.collect.platform_errors.log_platform"


def fetch(runtime: Runtime, *, source: str, method: str, transport: Transport, since: datetime,
          until: datetime) -> LogRead:
    section = runtime.settings.section(source)
    if not section.get("query"):
        raise SourceMisconfigured(f'没有访问日志的查询：在 controls."{source}".query 写日志平台上的查询')
    if not methods.exists(LOG_METHODS, method):
        raise SourceMisconfigured(f"没有这个日志平台方法：{method}")
    loaded = methods.load(LOG_METHODS, method)
    configured = methods.configure(loaded, settings=runtime.settings, source=source, secrets=runtime.secrets)
    timeout_s = runtime.settings.duration("limits.timeouts.http")
    return loaded.module.read(configured, transport=transport, timeout_s=timeout_s,
                              query=section["query"], since=since, until=until, limit=int(section["limit"]),
                              now=runtime.clock.now())
