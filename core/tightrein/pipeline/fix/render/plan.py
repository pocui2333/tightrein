"""修复计划中需要用户特别关注的事项与三类标记的名称；计划文档 plan.md 由 render/documents.py 按 plan 类型渲染。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FLAG_LABELS = {"design": "根因在设计本身", "dataStructure": "数据结构或存量数据", "publicContract": "公共实现或接口契约"}


def attention(plan: Mapping[str, Any]) -> list[str]:
    """需要用户特别关注的事项，计划确认的说明中逐项列出。"""
    found = []
    later = (plan.get("split") or {}).get("followUps") or []
    if later:
        found.append(f"拆分为 {len(later) + 1} 个子任务：本计划只做第 1 个，确认后其余 {len(later)} 个生成为排队的后续 "
                     "Issue，依次在前一个合并后开始")
    found += [f"受保护文件 {item['path']}：{item['change']}(理由：{item['reason']})" for item in plan["protectedTouches"]]
    found += [f"新增依赖 {item['name']} {item['version']}：{item['reason']}" for item in plan["newDependencies"]]
    found += [f"删除文件 {item['path']}：{item['reason']}" for item in plan["deletions"]]
    if plan["migration"] is not None:
        migration = plan["migration"]
        found.append(f"数据结构变更，新增迁移 {'、'.join(migration['entries'])}；"
                     f"{'可以' if migration['reversible'] else '不能'}撤销：{migration['revertMethod']}")
    found += [f"{FLAG_LABELS[name]}：{flag.get('reason')}" for name, flag in plan["flags"].items() if flag["flagged"]]
    found += [f"待定：{item['question']}(推荐 {item['recommendation']})" for item in plan["userDecisions"]]
    return found
