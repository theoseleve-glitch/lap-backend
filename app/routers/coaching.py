"""Public coaching endpoints (T202+).

Read-only surface over the coaching rules library. Auth-free —
ADR-009 ships the same rules to clients via the bundle endpoint
(T204) anyway, so the index is non-sensitive reference data the
web app can fetch before login.

Companion to the admin simulate endpoint (T201): admin tests rule
behavior against a hand-crafted feature dict; public callers list
the rule reference shape (id/version/category/subcategory/difficulty).
Full rule content (templates, conditions, prescriptions) only ships
via the bundle endpoint, not here.
"""

from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from lap_schemas.v1 import CoachingRuleList, CoachingRulePublic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.deps import get_config, get_current_user, get_db
from app.error_codes import ErrorCode
from coaching.library.bundle import sign_bundle
from coaching.library.compiler import compile_library
from coaching.library.rule_loader import load_rules
from infra.orm import CoachingInsight as CoachingInsightRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/coaching", tags=["coaching"])

_RULES_DIR = (
    Path(__file__).resolve().parents[2]
    / "coaching"
    / "library"
    / "rules"
)


@router.get("/rules", response_model=CoachingRuleList)
def list_rules() -> CoachingRuleList:
    rules = load_rules(_RULES_DIR)
    items = tuple(
        CoachingRulePublic(
            id=rule.id,
            version=rule.version,
            category=rule.category,
            subcategory=rule.subcategory,
            difficulty=rule.difficulty,
        )
        for rule in rules
    )
    return CoachingRuleList(items=items, total=len(items))


# T204 — full compiled+signed bundle for client engines.
# `version` only accepts "latest" today; the param exists for
# T-future to support pinned-version retrieval without breaking
# the wire shape now.
_SUPPORTED_BUNDLE_VERSIONS = frozenset({"latest"})


@router.get("/rules/bundle")
def get_rules_bundle(
    cfg: Annotated[Config, Depends(get_config)],
    version: str = Query(default="latest"),
) -> dict[str, Any]:
    if version not in _SUPPORTED_BUNDLE_VERSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported_bundle_version: {version!r}",
        )
    secret = cfg.coaching_bundle_signing_secret.get_secret_value()
    if not secret:
        # Refusing to ship unsigned is the correct posture — easier
        # to debug a 503 than discover later that production has
        # been serving unsigned payloads. Mirrors stripe webhook 503.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorCode.COACHING_BUNDLE_SIGNING_SECRET_NOT_CONFIGURED,
        )
    rules = load_rules(_RULES_DIR)
    unsigned = compile_library(rules)
    return sign_bundle(unsigned, secret=secret.encode("utf-8"))


# T205 — false-positive flag. Driver who owns the session that
# produced the insight POSTs here; we set
# coaching_insights.flagged_false_positive=True. T206 aggregates
# these flags per rule_id to surface mis-firing rules to admins.
# Idempotent: re-flagging a flagged insight is a 200 no-op so
# T206's aggregation doesn't double-count an accidental
# double-click.


@router.post("/insights/{insight_id}/flag")
async def flag_insight_false_positive(
    insight_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    insight = (
        await db.execute(
            select(CoachingInsightRow).where(
                CoachingInsightRow.id == insight_id
            )
        )
    ).scalar_one_or_none()
    # Both unknown and not-owned collapse to 404 — same posture as
    # sessions.get_session — so we don't leak existence to non-owners.
    if (
        insight is None
        or profile is None
        or insight.driver_id != profile.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.INSIGHT_NOT_FOUND,
        )
    if not insight.flagged_false_positive:
        insight.flagged_false_positive = True
        await db.commit()
    return {
        "id": str(insight.id),
        "flagged_false_positive": insight.flagged_false_positive,
    }
