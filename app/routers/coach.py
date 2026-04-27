"""T172-T173 (sprint2-backend) — coach-initiated endpoints.

Hosts coach-action endpoints that are distinct from the driver
self-view paths.

T172 — POST /api/v1/coach/prescriptions
  Coach assigns a manual prescription to a driver in their org.
  Status starts as "proposed"; driver still has to accept via
  the existing accept-prescription path. expected_context and
  success_condition default to empty dicts because a coach-
  assigned drill is reviewed manually rather than tracked
  automatically.

T173 — POST /api/v1/sessions/:id/coach-notes
  Coach attaches a free-text note to a driver's session. Author
  identity (`author_user_id`) is captured server-side from the
  JWT. Multiple notes per session are by design (no UNIQUE on
  session_id). Lives here under the `sessions_router` because it
  uses the `/api/v1/sessions` prefix; functionally it's coach-
  action so the implementation is in this file.

Authz model:
  - caller must share an org with the target driver, with role
    admin or coach
  - status parity 403 across unauthorized cases (not-member,
    driver-caller, driver-not-in-org, target-not-driver, unknown
    session) so existence isn't leaked
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

import uuid6
from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import (
    CoachPrescriptionCreate,
    CoachSessionNote,
    CoachSessionNoteCreate,
)
from lap_schemas.v1.action_prescription import (
    ActionPrescriptionListItem,
    PrescriptionPriority,
    PrescriptionStatus,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import ActionPrescription as ActionPrescriptionRow
from infra.orm import CoachSessionNote as CoachSessionNoteRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import OrganizationMember as OrganizationMemberRow
from infra.orm import Session as SessionRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/coach", tags=["coach"])
sessions_router = APIRouter(prefix="/api/v1/sessions", tags=["coach"])


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


@sessions_router.post(
    "/{session_id}/coach-notes",
    response_model=CoachSessionNote,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "not_authorized"},
    },
)
async def create_coach_session_note(
    session_id: UUID,
    body: CoachSessionNoteCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CoachSessionNote:
    """T173 — coach attaches a free-text note to a driver's
    session.

    Authz: caller must share an org with the session's driver,
    with role admin or coach. Drivers (even of the session)
    can't self-annotate via this endpoint — it's the coach<->
    driver feedback channel.

    Implementation: resolve session → driver_profile → driver
    user → look up the intersection of (caller's orgs as admin
    or coach) and (driver's orgs as driver). If no shared org
    pairing exists, 403 (status parity with unknown session).
    """
    session_row = (
        await db.execute(
            select(SessionRow).where(SessionRow.id == session_id)
        )
    ).scalar_one_or_none()
    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_authorized",
        )
    driver_profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.id == session_row.driver_id
            )
        )
    ).scalar_one_or_none()
    if driver_profile is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_authorized",
        )

    # Find any org where (a) caller is admin/coach AND (b)
    # driver is role=driver. EXISTS-style: a single shared org
    # qualifying both pairings is enough.
    caller_org_ids = set(
        (
            await db.execute(
                select(OrganizationMemberRow.org_id).where(
                    OrganizationMemberRow.user_id == user.id,
                    OrganizationMemberRow.role.in_(
                        ("admin", "coach")
                    ),
                )
            )
        ).scalars()
    )
    driver_org_ids = set(
        (
            await db.execute(
                select(OrganizationMemberRow.org_id).where(
                    OrganizationMemberRow.user_id
                    == driver_profile.user_id,
                    OrganizationMemberRow.role == "driver",
                )
            )
        ).scalars()
    )
    if not (caller_org_ids & driver_org_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_authorized",
        )

    note = CoachSessionNoteRow(
        id=uuid6.uuid7(),
        session_id=session_id,
        author_user_id=user.id,
        body=body.body,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return CoachSessionNote(
        id=note.id,
        session_id=note.session_id,
        author_user_id=note.author_user_id,
        body=note.body,
        created_at=note.created_at,
    )
