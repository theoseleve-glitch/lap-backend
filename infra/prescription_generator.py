"""T211 — Prescription generator. T212 — drill linkage.
T213 — success_condition population. T214 — dedup against episode.

Writes one ActionPrescription row linked to the WeaknessEpisode
that triggered it. T211 nails the basic creation path; T212
copies `rule.prescription.drill_template_id` through so the UI
can deep-link to the drill page; T213 dumps the rule's
SuccessCondition (metric/comparison/target_pct/measurement_window)
into the success_condition JSON column so the outcome tracker
has a machine-checkable contract; T214 makes the call idempotent
so a Celery retry doesn't spawn a second row — if a prescription
already exists for this episode, return its id and skip the insert.

`status='proposed'`: awaiting driver review. T215/T216 flip to
accepted/skipped via user-facing endpoints.

`priority='p1'` is the cautious default — 'before next session'
in ADR-005 terms. Severity-based escalation to p0 will land in a
later task; for now the operator can adjust manually.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import uuid6
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coaching.library.rule_schema import RuleSpec
from domain.target_match import compute_target_aware_priority
from infra.orm import ActionPrescription, WeaknessEpisode

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
    target_event: dict[str, Any] | None = None,
    candidate_track_id: UUID | None = None,
    candidate_car_id: UUID | None = None,
) -> UUID:
    """Create one ActionPrescription for `episode`, return its id.
    Idempotent: if a prescription already exists for this episode,
    return the existing id without inserting a second row.

    T090: when target_event + candidate_track_id/car_id are
    provided AND match the pinned event, priority is bumped
    from "p1" to "p0". All three kwargs default to None for
    backwards-compatibility with callers that don't yet thread
    target context through.
    """
    existing_id = (
        await db.execute(
            select(ActionPrescription.id).where(
                ActionPrescription.weakness_episode_id == episode.id
            )
        )
    ).scalar_one_or_none()
    if existing_id is not None:
        return existing_id
    new_id = uuid6.uuid7()
    priority = compute_target_aware_priority(
        target_event=target_event,
        candidate_track_id=candidate_track_id,
        candidate_car_id=candidate_car_id,
    )
    db.add(
        ActionPrescription(
            id=new_id,
            driver_id=episode.driver_id,
            weakness_episode_id=episode.id,
            created_at=now,
            priority=priority,
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
