"""T206 — false-positive flag aggregation.

Pure compute over `coaching_insights` rows: per rule_id, count
total insights and the subset where users hit the T205 flag
endpoint. Rate = flagged / total. Returns rules whose rate
exceeds `threshold` (default 0.10 per the sprint spec).

Sorted high-to-low by rate so the worst offenders surface first
in the admin UI. The aggregation runs in one GROUP BY query so
it stays cheap as the insight count grows.

No min-sample-size guard: the sprint contract is exactly
">10% rate". Operators see `total` and `flagged_count` in the
result and can decide whether a 1/1 spike is real.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.kpi_report import SlackPoster
from infra.orm import CoachingInsight

DEFAULT_THRESHOLD = 0.10


@dataclass(frozen=True)
class FlaggedRuleStat:
    rule_id: str
    total: int
    flagged_count: int
    false_positive_rate: float


async def compute_high_false_positive_rules(
    db: AsyncSession, *, threshold: float = DEFAULT_THRESHOLD
) -> list[FlaggedRuleStat]:
    """Aggregate flagged_false_positive across coaching_insights
    grouped by rule_id. Returns only rules above `threshold`,
    sorted high-to-low by rate."""
    flagged_sum = func.sum(
        case((CoachingInsight.flagged_false_positive, 1), else_=0)
    )
    total_count = func.count(CoachingInsight.id)
    stmt = select(
        CoachingInsight.rule_id,
        total_count.label("total"),
        flagged_sum.label("flagged_count"),
    ).group_by(CoachingInsight.rule_id)
    rows = (await db.execute(stmt)).all()

    stats: list[FlaggedRuleStat] = []
    for rule_id, total, flagged_count in rows:
        if total == 0:
            continue
        rate = float(flagged_count) / float(total)
        if rate > threshold:
            stats.append(
                FlaggedRuleStat(
                    rule_id=rule_id,
                    total=int(total),
                    flagged_count=int(flagged_count),
                    false_positive_rate=rate,
                )
            )
    stats.sort(key=lambda s: s.false_positive_rate, reverse=True)
    return stats


# T012 (sprint2-backend) — proactive Slack alert wrapping the
# aggregation. Mirrors `infra.session_audit.send_session_loss_alert`:
# silent on quiet days (no rules over threshold = no post), single
# multi-line ping when there ARE flagged rules so the operator gets
# the full picture without having to visit the admin UI.
def _format_alert(stats: Sequence[FlaggedRuleStat]) -> str:
    count = len(stats)
    plural = "rule" if count == 1 else "rules"
    lines = [
        f":warning: Coaching rule FP audit: *{count} {plural} over threshold*"
    ]
    for s in stats:
        pct = s.false_positive_rate * 100.0
        lines.append(
            f"• `{s.rule_id}` — {pct:.1f}% "
            f"({s.flagged_count}/{s.total} flagged)"
        )
    return "\n".join(lines)


async def send_high_fp_rules_alert(
    db: AsyncSession,
    slack: SlackPoster,
    *,
    channel: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> None:
    """Aggregate → alert if any. Skips the post entirely on a clean run."""
    stats = await compute_high_false_positive_rules(
        db, threshold=threshold
    )
    if not stats:
        return
    slack.post(channel, _format_alert(stats))


# T013 (sprint2-backend) — full per-rule stats for the admin
# rules-health overview. Different from `compute_high_false_positive_rules`:
# no threshold filter, EVERY rule_id seen in coaching_insights
# appears (even at 0% FP). The router merges this with the loaded
# rule library so rules that never fired also surface.
async def compute_per_rule_stats(
    db: AsyncSession,
) -> list[FlaggedRuleStat]:
    """Return per-rule_id stats for every rule that has at least
    one insight in the DB. Sorted FP rate desc."""
    flagged_sum = func.sum(
        case((CoachingInsight.flagged_false_positive, 1), else_=0)
    )
    total_count = func.count(CoachingInsight.id)
    stmt = select(
        CoachingInsight.rule_id,
        total_count.label("total"),
        flagged_sum.label("flagged_count"),
    ).group_by(CoachingInsight.rule_id)
    rows = (await db.execute(stmt)).all()

    stats: list[FlaggedRuleStat] = []
    for rule_id, total, flagged_count in rows:
        if total == 0:
            continue
        rate = float(flagged_count) / float(total)
        stats.append(
            FlaggedRuleStat(
                rule_id=rule_id,
                total=int(total),
                flagged_count=int(flagged_count),
                false_positive_rate=rate,
            )
        )
    stats.sort(key=lambda s: s.false_positive_rate, reverse=True)
    return stats
