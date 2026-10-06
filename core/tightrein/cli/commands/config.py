"""config show [--key <键>](architecture/01 5.1)：合成后每个键的生效值与来源层。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import group, leaf
from tightrein.cli.output import Outcome
from tightrein.config import show as config_show


def _show(invocation: Any) -> Outcome:
    app = invocation.app
    key = invocation.args.key
    shown = config_show.show(app.config, app.user, key)
    if key is not None and not shown:
        raise exit_codes.UsageError(f"没有配置项 {key}")
    lines = [f"{item.key} = {json.dumps(item.value, ensure_ascii=False, default=str)}  [{item.source}]"
             for item in shown]
    if key is not None:
        lines += [f"  {item.key} 在 {layer} 层：{json.dumps(value, ensure_ascii=False, default=str)}"
                  for item in shown for layer, value in item.layers.items()]
    return Outcome("config show", exit_codes.OK, lines, result=[item.to_dict() for item in shown])


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    shown = leaf(commands, common, "config", _show, "生效的配置值与来源层")
    shown.add_argument("--key", help="只显示该键及其下级键，并列出各层中的值")
