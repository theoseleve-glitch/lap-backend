"""Admin endpoints (T161+).

`is_admin` flag on User is the gate. Tools here are operational —
diagnose drift, force-sync from Stripe, manually trigger reconciles.
Not exposed to regular users.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db, require_admin
from app.error_codes import ErrorCode
from app.logging import logger
from coaching.library.rule_runner import (
    InsightRenderError,
    run_rules,
)
from coaching.library.rule_schema import Difficulty
from infra.admin_audit import record_admin_action
from infra.coaching_flag_aggregation import (
    compute_high_false_positive_rules,
    compute_per_rule_stats,
)
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import Session as SessionRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# T201 — admin can dry-run the coaching rule library against a
# hand-crafted feature dict. Resolves the canonical rules tree
# at the package root so this stays accurate if the file moves.
_RULES_DIR = (
    Path(__file__).resolve().parents[2]
    / "coaching"
    / "library"
    / "rules"
)


class RefundRequest(BaseModel):
    """Operator-issued refund. `amount` (in the smallest currency unit,
    e.g. cents) is optional — omitting it asks Stripe for a full
    refund of the original charge."""

    charge_id: str = Field(..., min_length=1)
    amount: int | None = Field(default=None, ge=1)


def _require_admin(user: UserRow) -> None:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.ADMIN_ONLY,
        )


@router.post("/billing/sync/{user_id}")
async def force_sync_billing_status(
    user_id: UUID,
    caller: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Pull the most-recent Stripe subscription for the target
    user's customer and write its `status` to the local DB. If the
    customer has no subs, leave status unchanged (admin sync isn't
    a "reset to default" tool)."""
    _require_admin(caller)
    target = (
        await db.execute(select(UserRow).where(UserRow.id == user_id))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.USER_NOT_FOUND,
        )
    if not target.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ErrorCode.NO_STRIPE_CUSTOMER,
        )
    listing = stripe.Subscription.list(
        customer=target.stripe_customer_id, limit=1
    )
    found_status: str | None = None
    for sub_any in listing.auto_paging_iter():
        sub: Any = sub_any
        found_status = str(sub.get("status") or "") or None
        break
    if found_status:
        target.subscription_status = found_status
        logger.info(
            "admin_billing_sync_applied",
            user_id=str(target.id),
            new_status=found_status,
        )
    else:
        logger.info(
            "admin_billing_sync_no_subs",
            user_id=str(target.id),
        )
    # T197 — audit row regardless of whether a sub was found:
    # the admin still performed the sync action, and the audit
    # answers "an admin attempted to reconcile this account".
    await record_admin_action(
        db,
        admin_user_id=caller.id,
        action="billing.sync",
        target_id=target.id,
        metadata={"new_status": found_status},
    )
    await db.commit()
    return {"subscription_status": target.subscription_status}


@router.post("/billing/refund")
async def refund_charge(
    body: RefundRequest,
    caller: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str | int]:
    """Issue a Stripe refund against a charge. Full refund by default;
    pass `amount` (smallest currency unit) for a partial. The
    endpoint is a thin relay against Stripe + an audit row in
    admin_audit_log (T197) so the money-moving action is durably
    attributable."""
    _require_admin(caller)
    kwargs: dict[str, Any] = {"charge": body.charge_id}
    if body.amount is not None:
        kwargs["amount"] = body.amount
    refund: Any = stripe.Refund.create(**kwargs)
    refund_id = str(refund.get("id") or "")
    refund_status = str(refund.get("status") or "")
    refund_amount = int(refund.get("amount") or 0)
    logger.info(
        "admin_billing_refund_issued",
        admin_user_id=str(caller.id),
        charge_id=body.charge_id,
        refund_id=refund_id,
        amount=refund_amount,
        status=refund_status,
    )
    # T197 — durable audit. target_id is None: charge ids are
    # Stripe-side opaque strings, not UUIDs in our DB. The
    # charge_id + refund_id live in metadata where regulators
    # can cross-reference Stripe's ledger.
    await record_admin_action(
        db,
        admin_user_id=caller.id,
        action="billing.refund",
        target_id=None,
        metadata={
            "charge_id": body.charge_id,
            "refund_id": refund_id,
            "amount": refund_amount,
            "status": refund_status,
        },
    )
    await db.commit()
    return {
        "refund_id": refund_id,
        "status": refund_status,
        "amount": refund_amount,
    }


# T178 — operator KPI dashboard. The full ADR definitions need
# PostHog event joins (e.g., "viewed coaching surface"); this is
# the local-DB approximation good enough for an at-a-glance view.
_KPI_WINDOW = timedelta(days=7)
_PAID_STATUSES = ("active", "trialing")
_READY_SESSION_STATUS = "ready"


@router.get("/kpis")
async def admin_kpis(
    caller: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Returns the three glance-able KPIs over the last 7 days:
    WPD (paid users with at least one session), activation rate
    (signups that uploaded a session), and upload success rate
    (ready sessions / total)."""
    _require_admin(caller)
    threshold = datetime.now(UTC) - _KPI_WINDOW

    wpd = (
        await db.execute(
            select(func.count(func.distinct(UserRow.id)))
            .join(
                DriverProfileRow,
                DriverProfileRow.user_id == UserRow.id,
            )
            .join(
                SessionRow,
                SessionRow.driver_id == DriverProfileRow.id,
            )
            .where(UserRow.subscription_status.in_(_PAID_STATUSES))
            .where(SessionRow.created_at >= threshold)
        )
    ).scalar_one()

    total_signups = (
        await db.execute(
            select(func.count(UserRow.id)).where(
                UserRow.created_at >= threshold
            )
        )
    ).scalar_one()
    activated_signups = (
        await db.execute(
            select(func.count(func.distinct(UserRow.id)))
            .join(
                DriverProfileRow,
                DriverProfileRow.user_id == UserRow.id,
            )
            .join(
                SessionRow,
                SessionRow.driver_id == DriverProfileRow.id,
            )
            .where(UserRow.created_at >= threshold)
        )
    ).scalar_one()
    activation_rate = (
        activated_signups / total_signups if total_signups > 0 else 0.0
    )

    total_sessions = (
        await db.execute(
            select(func.count(SessionRow.id)).where(
                SessionRow.created_at >= threshold
            )
        )
    ).scalar_one()
    ready_sessions = (
        await db.execute(
            select(func.count(SessionRow.id))
            .where(SessionRow.created_at >= threshold)
            .where(SessionRow.status == _READY_SESSION_STATUS)
        )
    ).scalar_one()
    upload_success_rate = (
        ready_sessions / total_sessions if total_sessions > 0 else 1.0
    )

    return {
        "wpd": int(wpd),
        "activation_rate": float(activation_rate),
        "upload_success_rate": float(upload_success_rate),
        "computed_at": datetime.now(UTC).isoformat(),
    }


# T201 — admin dry-run for the coaching rules engine. Use case:
# operator edits a rule's threshold or template, wants to verify
# the rule fires (or doesn't) against a known feature dict
# without waiting for a real driver session. Reloads the rules
# tree on every call so YAML edits take effect without a server
# restart — this endpoint is admin-only and rare, so the cost of
# the file walk is fine.


class _RuleSimulateRequest(BaseModel):
    context: dict[str, Any] = Field(
        ...,
        description=(
            "Feature dict, same shape rule_runner.run_rules expects. "
            "Numeric features for detection conditions plus any "
            "string/number vars referenced by `{{var}}` placeholders "
            "in the rule's explanation_template."
        ),
    )
    difficulty: Difficulty = "beginner"


class _InsightOut(BaseModel):
    rule_id: str
    rule_version: int
    category: str
    subcategory: str
    difficulty: Difficulty
    explanation: str


class _RuleSimulateResponse(BaseModel):
    insights: list[_InsightOut]


@router.post(
    "/coaching/rules/simulate", response_model=_RuleSimulateResponse
)
def simulate_coaching_rules(
    body: _RuleSimulateRequest,
    _admin: Annotated[UserRow, Depends(require_admin)],
) -> _RuleSimulateResponse:
    # Lazy import: avoids loading every YAML on app import for the
    # 99% of requests that don't hit this endpoint, and lets the
    # admin pick up rule edits without a process restart.
    from coaching.library.rule_loader import load_rules

    rules = load_rules(_RULES_DIR)
    try:
        insights = run_rules(
            rules, context=body.context, difficulty=body.difficulty
        )
    except InsightRenderError as exc:
        # Surface the rule_id + missing var in the HTTP error so
        # the operator can fix the offending YAML in one step
        # rather than tailing logs for the full traceback.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _RuleSimulateResponse(
        insights=[
            _InsightOut(
                rule_id=ins.rule_id,
                rule_version=ins.rule_version,
                category=ins.category,
                subcategory=ins.subcategory,
                difficulty=ins.difficulty,
                explanation=ins.explanation,
            )
            for ins in insights
        ]
    )


# T206 — surfaces rules whose users-flagged-as-false-positive
# rate exceeds 10%. Pure compute over coaching_insights — no
# cron, no cached state. Operator visits this when reviewing
# rule library health, sees the worst offenders sorted first.


class _FlaggedRuleOut(BaseModel):
    rule_id: str
    total: int
    flagged_count: int
    false_positive_rate: float


class _FlaggedRulesResponse(BaseModel):
    rules: list[_FlaggedRuleOut]


@router.get(
    "/coaching/rules/flagged", response_model=_FlaggedRulesResponse
)
async def list_flagged_coaching_rules(
    _admin: Annotated[UserRow, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> _FlaggedRulesResponse:
    stats = await compute_high_false_positive_rules(db)
    return _FlaggedRulesResponse(
        rules=[
            _FlaggedRuleOut(
                rule_id=s.rule_id,
                total=s.total,
                flagged_count=s.flagged_count,
                false_positive_rate=s.false_positive_rate,
            )
            for s in stats
        ]
    )


# T013 (sprint2-backend) — comprehensive rules health overview.
# Distinct from /coaching/rules/flagged (T206): no threshold filter,
# every library rule appears (even at 0% FP, even if it never fired)
# AND every rule_id ever seen in coaching_insights appears (even if
# the library no longer ships it — e.g. after a rename).
#
# Sort: rules WITH stats sorted by FP rate desc; rules with no
# data (total=0) appended at the end so the operator's eye lands
# on the worst offenders first.


class _RuleHealthOut(BaseModel):
    rule_id: str
    total: int
    flagged_count: int
    false_positive_rate: float


class _RulesHealthResponse(BaseModel):
    rules: list[_RuleHealthOut]


@router.get("/rules/health", response_model=_RulesHealthResponse)
async def list_rules_health(
    _admin: Annotated[UserRow, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> _RulesHealthResponse:
    # Lazy import — same reasoning as /coaching/rules/simulate:
    # avoids loading every YAML on app import for the 99% of
    # requests that don't hit this endpoint.
    from coaching.library.rule_loader import load_rules

    library = load_rules(_RULES_DIR)
    db_stats = await compute_per_rule_stats(db)
    seen = {s.rule_id for s in db_stats}

    rows: list[_RuleHealthOut] = []
    # First: rules with stats — sorted by FP rate desc by the
    # aggregation function. Operator's eye lands on worst first.
    # rule_ids in DB but not in the library (e.g. after a rule
    # rename) appear here too at their natural FP-rate position.
    for stat in db_stats:
        rows.append(
            _RuleHealthOut(
                rule_id=stat.rule_id,
                total=stat.total,
                flagged_count=stat.flagged_count,
                false_positive_rate=stat.false_positive_rate,
            )
        )
    # Then: library rules with no insights yet — total=0,
    # rate=0. Operator can spot dead rules.
    for rule in library:
        if rule.id in seen:
            continue
        rows.append(
            _RuleHealthOut(
                rule_id=rule.id,
                total=0,
                flagged_count=0,
                false_positive_rate=0.0,
            )
        )
    return _RulesHealthResponse(rules=rows)


# T064 (sprint2-backend) — operator visibility into cohort sizes
# per (master_car, master_track). One entry per distinct cohort
# that has any contributions in cohort_metrics; unlocked flag
# echoes the T056 threshold check so the operator can spot
# cohorts that are nearly across the line.


class _CohortStatusEntry(BaseModel):
    car_id: UUID
    track_id: UUID
    cohort_size: int
    unlocked: bool


class _CohortStatusResponse(BaseModel):
    cohorts: list[_CohortStatusEntry]


@router.get("/cohort/status", response_model=_CohortStatusResponse)
async def list_cohort_status(
    _admin: Annotated[UserRow, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> _CohortStatusResponse:
    # Lazy import — keeps the cohort domain out of the admin
    # module's import surface for the 99% of admin requests
    # that don't hit this endpoint.
    from domain.cohort_minimum import (
        MIN_COHORT_SIZE,
        count_cohort_members,
    )
    from infra.orm import CohortMetric

    distinct_cohorts = (
        (
            await db.execute(
                select(CohortMetric.car_id, CohortMetric.track_id)
                .distinct()
            )
        )
        .all()
    )
    entries: list[_CohortStatusEntry] = []
    for car_id, track_id in distinct_cohorts:
        n = await count_cohort_members(
            db, car_id=car_id, track_id=track_id
        )
        entries.append(
            _CohortStatusEntry(
                car_id=car_id, track_id=track_id,
                cohort_size=n, unlocked=n >= MIN_COHORT_SIZE,
            )
        )
    return _CohortStatusResponse(cohorts=entries)
