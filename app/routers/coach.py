"""T172 (sprint2-backend) — coach-initiated endpoints.

Hosts coach-action endpoints that are distinct from the driver
self-view paths. Currently:

  - POST /api/v1/coach/prescriptions — coach assigns a manual
    prescription to a driver in their org. Status starts as
    "proposed"; driver still has to accept via the existing
    accept-prescription path. expected_context and
    success_condition default to empty dicts because a coach-
    assigned drill is reviewed manually rather than tracked
    automatically.

Authz model:
  - caller must be admin or coach in the named org
  - target must be a driver-role member of the same org
  - status parity 403 across unauthorized cases (not-member,
    driver-caller, driver-not-in-org, target-not-driver)

The org membership lookup doubles as the existence check, so an
unknown org_id surfaces as the same 403 a non-member would —
no org-existence leak.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast

import uuid6
from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import CoachPrescriptionCreate
from lap_schemas.v1.action_prescription import (
    ActionPrescriptionListItem,
    PrescriptionPriority,
    PrescriptionStatus,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import ActionPrescription as ActionPrescriptionRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import OrganizationMember as OrganizationMemberRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/coach", tags=["coach"])


@router.post(
    "/prescriptions",
    response_model=ActionPrescriptionListItem,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "not_authorized"},
    },
)
async def create_coach_prescription(
    body: CoachPrescriptionCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ActionPrescriptionListItem:
    caller_membership = (
        await db.execute(
            select(OrganizationMemberRow).where(
                OrganizationMemberRow.org_id == body.org_id,
                OrganizationMemberRow.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if caller_membership is None or caller_membership.role == "driver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_authorized",
        )

    target_membership = (
        await db.execute(
            select(OrganizationMemberRow).where(
                OrganizationMemberRow.org_id == body.org_id,
                OrganizationMemberRow.user_id == body.driver_user_id,
            )
        )
    ).scalar_one_or_none()
    if (
        target_membership is None
        or target_membership.role != "driver"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_authorized",
        )

    target_profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == body.driver_user_id
            )
        )
    ).scalar_one_or_none()
    if target_profile is None:
        # Member of the org as role=driver but no DriverProfile
        # row yet. Fall back to 403 with the same status parity:
        # the driver hasn't completed onboarding so we can't
        # attach a prescription. Surfaces alongside the other
        # not_authorized cases to keep the response uniform.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_authorized",
        )

    now = datetime.now(UTC)
    rx = ActionPrescriptionRow(
        id=uuid6.uuid7(),
        driver_id=target_profile.id,
        weakness_episode_id=None,
        created_at=now,
        priority=body.priority,
        title=body.title,
        rationale=body.rationale,
        drill_template_id=body.drill_template_id,
        # Coach-assigned drills are reviewed manually; structured
        # context and success-condition default to empty dicts.
        expected_context={},
        success_condition={},
        status="proposed",
    )
    db.add(rx)
    await db.commit()
    await db.refresh(rx)
    return ActionPrescriptionListItem(
        id=rx.id,
        driver_id=rx.driver_id,
        weakness_episode_id=rx.weakness_episode_id,
        created_at=rx.created_at,
        priority=cast(PrescriptionPriority, rx.priority),
        title=rx.title,
        rationale=rx.rationale,
        drill_template_id=rx.drill_template_id,
        success_condition=rx.success_condition,
        status=cast(PrescriptionStatus, rx.status),
        accepted_at=rx.accepted_at,
    )
