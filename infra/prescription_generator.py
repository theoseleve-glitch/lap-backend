"""T211 — Prescription generator. T212 — drill linkage.
T213 — success_condition population.

Writes one ActionPrescription row linked to the WeaknessEpisode
that triggered it. T211 nails the basic creation path; T212
copies `rule.prescription.drill_template_id` through so the UI
can deep-link to the drill page; T213 dumps the rule's
SuccessCondition (metric/comparison/target_pct/measurement_window)
into the success_condition JSON column so the outcome tracker
has a machine-checkable contract. T214 adds dedup so the same
open episode doesn't spawn duplicate prescriptions.

`status='proposed'`: awaiting driver review. T215/T216 flip to
accepted/skipped via user-facing endpoints.

`priority='p1'` is the cautious default — 'before next session'
in ADR-005 terms. Severity-based escalation to p0 will land in a
later task; for now the operator can adjust manually.
"""

from datetime import datetime
from uuid import UUID

import uuid6
from sqlalchemy.ext.asyncio import AsyncSession

from coaching.library.rule_schema import RuleSpec
from infra.orm import ActionPrescription, WeaknessEpisode

_DEFAULT_PRIORITY = "p1"
_INITIAL_STATUS = "proposed"


def _title(rule: RuleSpec) -> str:
    """Short UI label. Subcategory like 'late_braking' becomes
    'Address late braking'. Plain enough that an operator can
    scan a list of prescriptions without expanding each one."""
    pretty = rule.subcategory.replace("_", " ")
    return f"Address {pretty}"


def _rationale(rule: RuleSpec, episode: WeaknessEpisode) -> str:
    """Plain-language why. References the library rule and the
    recurrence count so the driver sees how persistent the issue
    has been. T-future may swap this for a richer template once
    the runtime can render with full context."""
    return (
        f"Library rule {rule.id} has fired "
        f"{episode.recurrence_count} time(s) on this driver. "
        f"Addressing it should improve {rule.subcategory.replace('_', ' ')}."
    )


async def generate_prescription_for_episode(
    db: AsyncSession,
    *,
    episode: WeaknessEpisode,
    rule: RuleSpec,
    now: datetime,
) -> UUID:
    """Create one ActionPrescription for `episode`, return its id."""
    new_id = uuid6.uuid7()
    db.add(
        ActionPrescription(
            id=new_id,
            driver_id=episode.driver_id,
            weakness_episode_id=episode.id,
            created_at=now,
            priority=_DEFAULT_PRIORITY,
            title=_title(rule),
            rationale=_rationale(rule, episode),
            expected_context={},
            success_condition=rule.prescription.success_condition.model_dump(
                mode="json"
            ),
            status=_INITIAL_STATUS,
            drill_template_id=rule.prescription.drill_template_id,
        )
    )
    await db.commit()
    return new_id
