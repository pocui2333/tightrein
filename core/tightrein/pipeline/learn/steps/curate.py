"""知识库的定期复核与整理(design 16.7，architecture/08 6.4)。

- 经验类条目(分诊经验、修复经验)自动处理(cleanup)：过了 reviewBy 且最近 retrieval.unusedDays 天没有被命中的归档，
  过了 reviewBy 但仍被命中的续期 thresholds.learn.lessonReviewDays 天；
- 其他类型过期与未命中的条目各一条 knowledge-review 建议，用户选择续期或归档；
- 同类型、标签相近的矛盾候选组逐组经 lesson-writer 比对，给出的矛盾组与重复组各一条建议，用户选择合并、归档或续期；
  比对没有结果的组记入 errors，不生成建议。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from tightrein.domain.enums import KnowledgeStatus, KnowledgeType, RunnerStatus, SuggestionKind
from tightrein.pipeline.learn.prompts.common import LearnEnv
from tightrein.pipeline.learn.prompts.lesson import compare_task
from tightrein.pipeline.learn.steps.suggestions import Draft

LESSON_TYPES = frozenset({KnowledgeType.TRIAGE_LESSON.value, KnowledgeType.FIX_LESSON.value})
ARCHIVED, RENEWED = "archived", "renewed"
OVERDUE_REASON = "已过复核日期"
UNUSED_REASON = "长期没有被命中"


def _draft(cause: str, ids: list[str], reason: str, **extra: object) -> Draft:
    return Draft(SuggestionKind.KNOWLEDGE_REVIEW, f"{cause}:{','.join(ids)}", {"ids": ids, "reason": reason, **extra},
                 tuple(ids))


def cleanup(env: LearnEnv) -> list[dict[str, Any]]:
    """经验类条目的自动归档与续期；--output 模式下只列出，不改动。"""
    report = env.knowledge.stale()
    unused = {hit.id for hit in report.unused}
    review_by = env.today() + timedelta(days=env.config.whole_threshold("learn.lessonReviewDays"))
    done = []
    for hit in report.overdue:
        if hit.type not in LESSON_TYPES:
            continue
        action = ARCHIVED if hit.id in unused else RENEWED
        if env.writable:
            if action == ARCHIVED:
                env.knowledge.set_status(hit.id, KnowledgeStatus.ARCHIVED)
            else:
                env.knowledge.set_status(hit.id, KnowledgeStatus.ACTIVE, review_by=review_by)
        done.append({"id": hit.id, "action": action})
    return done


def review_drafts(env: LearnEnv) -> tuple[list[Draft], list[dict[str, str]]]:
    report = env.knowledge.stale()
    drafts = [_draft("overdue", [hit.id], OVERDUE_REASON) for hit in report.overdue if hit.type not in LESSON_TYPES]
    drafts += [_draft("unused", [hit.id], UNUSED_REASON) for hit in report.unused if hit.type not in LESSON_TYPES]
    errors = []
    for number, group in enumerate(report.contradiction_groups, start=1):
        entries = [env.knowledge.get(hit.id, record_hit=False) for hit in group]
        result = env.calls.run(compare_task(env.calls.prompt, number, entries))
        if result.status is not RunnerStatus.OK or result.output is None:
            errors.append({"item": f"compare:{','.join(hit.id for hit in group)}",
                           "reason": f"比对没有结果：{result.status.value} {result.error_type or ''}".strip()})
            continue
        known = {hit.id for hit in group}
        for kind, key, items in (("contradiction", "facts", result.output["contradictions"]),
                                 ("duplicate", "reason", result.output["duplicates"])):
            for item in items:
                ids = [entry_id for entry_id in item["ids"] if entry_id in known]
                if len(ids) >= 2:
                    drafts.append(_draft(kind, ids, item[key], compared=True, runId=env.run_id))
    return drafts, errors
