"""命令：每个日常命令或分组一个文件，各自提供 register(commands, common, language)。顺序即帮助中的顺序。"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from tightrein.cli.commands import (
    admin,
    approve,
    control,
    knowledge,
    new,
    problem,
    project,
    retro,
    run,
    show,
    status,
    watch,
)

type Register = Callable[[argparse._SubParsersAction[argparse.ArgumentParser], argparse.ArgumentParser, str], None]

COMMANDS: tuple[Register, ...] = (
    status.register, watch.register, show.register, approve.register, approve.register_reject, new.register,
    run.register, *control.REGISTERS, project.register, problem.register, retro.register, knowledge.register,
    admin.register,
)
