"""T163-T164 (sprint2-backend) — organizations endpoints.

T163 — POST /api/v1/orgs creates an Organization.
T164 — POST /api/v1/orgs/:id/invites creates an invitation row.

T163 endpoint:
  - assigns admin_user_id to the calling user
  - auto-creates an OrganizationMember row with role="admin" for
    the creator (so the admin appears as a member from day 1
    rather than being implicit-only — keeps team-roster queries
    consistent without a UNION between Organization.admin_user_id
    and OrganizationMember.user_id)
  - returns 201 with the full Organization shape

T164 endpoint:
  - validates the caller is the org admin (403 otherwise)
  - validates the email isn't already a member (409)
  - validates members + pending invites < seats_limit (409)
  - persists an OrganizationInvitation with a 256-bit random
    token (secrets.token_urlsafe(32)) and 7-day TTL
  - returns 201 with the full invitation shape including token
    (T165 redeems by token; email delivery is a later task)

The schema-layer Pydantic enforces input bounds (name 1-120,
seats_limit >= 1, plan ∈ {free, team, pro}, invite role ∈
{coach, driver}, email shape sanity-checked) so the endpoint
body deliberately holds no validation logic — the boundary is
the schema, not the controller.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import uuid6
from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import Organization as OrganizationSchema
from lap_schemas.v1 import OrganizationCreate, OrganizationInvitationCreate
from lap_schemas.v1 import OrganizationInvitation as OrgInvitationSchema
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import Organization as OrganizationRow
from infra.orm import OrganizationInvitation as OrgInvitationRow
from infra.orm import OrganizationMember as OrganizationMemberRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/orgs", tags=["organizations"])

_INVITE_TTL_DAYS = 7


@router.post(
    "",
    response_model=OrganizationSchema,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    body: OrganizationCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrganizationSchema:
    org = OrganizationRow(
        id=uuid6.uuid7(),
        name=body.name,
        plan=body.plan,
        admin_user_id=user.id,
        seats_limit=body.seats_limit,
    )
    db.add(org)
    await db.flush()
    db.add(
        OrganizationMemberRow(
            id=uuid6.uuid7(),
            org_id=org.id,
            user_id=user.id,
            role="admin",
        )
    )
    await db.commit()
    await db.refresh(org)
    return OrganizationSchema(
        id=org.id,
        name=org.name,
        plan=org.plan,
        admin_user_id=org.admin_user_id,
        seats_limit=org.seats_limit,
        created_at=org.created_at,
    )


@router.post(
    "/{org_id}/invites",
    response_model=OrgInvitationSchema,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "not_org_admin"},
        409: {"description": "conflict"},
    },
)
async def create_organization_invitation(
    org_id: UUID,
    body: OrganizationInvitationCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrgInvitationSchema:
    org = (
        await db.execute(
            select(OrganizationRow).where(
                OrganizationRow.id == org_id
            )
        )
    ).scalar_one_or_none()
    if org is None or org.admin_user_id != user.id:
        # Status parity: non-existent and not-admin both look the
        # same to non-admin callers (no org-existence leak).
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_org_admin",
        )

    # Already-a-member check: join organization_members → users
    # on email. SQLite-portable subquery (no JOIN needed since
    # we have the email shape; lookup user-by-email then check
    # membership).
    existing_user = (
        await db.execute(
            select(UserRow).where(UserRow.email == body.email)
        )
    ).scalar_one_or_none()
    if existing_user is not None:
        is_member = (
            await db.execute(
                select(OrganizationMemberRow.id).where(
                    OrganizationMemberRow.org_id == org_id,
                    OrganizationMemberRow.user_id
                    == existing_user.id,
                )
            )
        ).scalar_one_or_none() is not None
        if is_member:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="already_member",
            )

    now = datetime.now(UTC)
    # Seat budget: members + non-expired pending invites.
    member_count = (
        await db.execute(
            select(func.count(OrganizationMemberRow.id)).where(
                OrganizationMemberRow.org_id == org_id
            )
        )
    ).scalar_one()
    pending_invite_count = (
        await db.execute(
            select(func.count(OrgInvitationRow.id)).where(
                OrgInvitationRow.org_id == org_id,
                OrgInvitationRow.accepted_at.is_(None),
                OrgInvitationRow.expires_at > now,
            )
        )
    ).scalar_one()
    if member_count + pending_invite_count >= org.seats_limit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="seats_exhausted",
        )

    invitation = OrgInvitationRow(
        id=uuid6.uuid7(),
        org_id=org_id,
        email=body.email,
        role=body.role,
        token=secrets.token_urlsafe(32),
        expires_at=now + timedelta(days=_INVITE_TTL_DAYS),
    )
    db.add(invitation)
    await db.commit()
    await db.refresh(invitation)
    return OrgInvitationSchema(
        id=invitation.id,
        org_id=invitation.org_id,
        email=invitation.email,
        role=invitation.role,
        token=invitation.token,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
    )
