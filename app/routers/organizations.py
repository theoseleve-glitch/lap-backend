"""T163 (sprint2-backend) — POST /api/v1/orgs create-org endpoint.

Authenticated user creates a new Organization. The endpoint:
  - assigns admin_user_id to the calling user
  - auto-creates an OrganizationMember row with role="admin" for
    the creator (so the admin appears as a member from day 1
    rather than being implicit-only — keeps team-roster queries
    consistent without a UNION between Organization.admin_user_id
    and OrganizationMember.user_id)
  - returns 201 with the full Organization shape

The schema-layer Pydantic enforces input bounds (name 1-120,
seats_limit >= 1, plan ∈ {free, team, pro}) so the endpoint body
deliberately holds no validation logic — the boundary is the
schema, not the controller.
"""

from __future__ import annotations

from typing import Annotated

import uuid6
from fastapi import APIRouter, Depends, status
from lap_schemas.v1 import Organization as OrganizationSchema
from lap_schemas.v1 import OrganizationCreate
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import Organization as OrganizationRow
from infra.orm import OrganizationMember as OrganizationMemberRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/orgs", tags=["organizations"])


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
