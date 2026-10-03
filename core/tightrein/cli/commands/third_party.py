"""第三方 skill 的锁定与校验(architecture/09 第 7 节)：third-party lock、third-party verify。

lock 取 commit(--ref 或来源仓库的最新 commit)、按 design 9.11 核实、下载到缓存并记录逐文件哈希；改写已锁定的条目时
列出文件差异，终端中输入 yes 后才写入清单，非交互调用停在关口。清单的提交由用户自行完成。
"""

from __future__ import annotations

import argparse
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import confirmed, group, leaf, not_confirmed, packaging_context
from tightrein.cli.output import Outcome, error
from tightrein.packaging import third_party


def _lock(invocation: Any) -> Outcome:
    args = invocation.args
    ctx = packaging_context(invocation)
    path = ctx.tool.third_party_lock()
    skills = ctx.lock()
    names = set(args.names)
    unknown = names - {skill.name for skill in skills}
    if unknown:
        raise third_party.LockError(f"锁定清单中没有：{', '.join(sorted(unknown))}")

    def head(source: str) -> str:
        return third_party.head_commit(ctx.process, ctx.tool.root, source)

    if args.dry_run:
        planned = [{"name": skill.name, "oldRef": skill.ref, "newRef": args.ref or head(skill.source)}
                   for skill in skills if not names or skill.name in names]
        lines = [f"将锁定 {item['name']}：{item['oldRef'] or '未锁定'} → {item['newRef']}" for item in planned]
        return Outcome("third-party lock", exit_codes.OK, lines or ["锁定清单为空"], result=planned)
    locked, changes = third_party.lock(
        skills, names, ref=args.ref, head=head,
        facts=lambda source: third_party.repo_facts(ctx.process, ctx.tool.root, source),
        fetch=ctx.fetch, cache=ctx.cache, config=ctx.config, clock=ctx.clock, zone=ctx.zone)
    if not changes:
        return Outcome("third-party lock", exit_codes.OK, ["锁定清单已是最新，没有改动"], result=[])
    lines = [change.describe() for change in changes]
    result = [change.to_dict() for change in changes]
    if any(change.replaces for change in changes) and not confirmed(invocation, "\n".join(["将改写锁定清单：", *lines])):
        if not invocation.interactive:
            return not_confirmed("third-party lock", lines, result)
        return Outcome("third-party lock", exit_codes.OK, ["已取消，锁定清单未改写"], result=result)
    third_party.write_lock(path, locked)
    return Outcome("third-party lock", exit_codes.OK,
                   [f"已写入 {path}", *lines, "锁定清单的提交由你自行完成"], result=result)


def _verify(invocation: Any) -> Outcome:
    ctx = packaging_context(invocation)
    issues, notes = [], []
    for skill in ctx.lock():
        if skill.ref is None:
            notes.append(f"{skill.name} 尚未锁定")
            continue
        issues += third_party.verify(skill, ctx.cache(skill.name, skill.ref))
    lines = [f"- {issue.describe()}" for issue in issues] + notes
    if issues:
        return Outcome("third-party verify", exit_codes.FAILED, [f"有 {len(issues)} 处与锁定清单不符", *lines],
                       result=[issue.to_dict() for issue in issues],
                       errors=[error("HashMismatch", issue.describe(), "缓存缺失时执行 tightrein install 下载")
                               for issue in issues])
    return Outcome("third-party verify", exit_codes.OK, ["第三方 skill 与锁定清单一致", *notes], result=[])


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    third = group(commands, "third-party", "第三方 skill 的锁定与校验")
    lock = leaf(third, common, "lock", _lock, "锁定或更新第三方 skill 的 commit 与哈希", "third-party lock")
    lock.add_argument("names", nargs="*", help="只锁定这些条目；省略时为全部")
    lock.add_argument("--ref", help="锁定到这个 commit(40 位)，只能与一个名称同用；省略时取来源仓库的最新 commit")
    leaf(third, common, "verify", _verify, "按锁定清单校验缓存中的第三方 skill", "third-party verify")
