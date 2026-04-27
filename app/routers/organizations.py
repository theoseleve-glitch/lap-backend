"""T163-T176 (sprint2-backend) — organizations endpoints.

T163 — POST   /api/v1/orgs creates an Organization.
T164 — POST   /api/v1/orgs/:id/invites creates an invitation row.
T165 — POST   /api/v1/orgs/:id/invites/:token/accept redeems it.
T166 — DELETE /api/v1/orgs/:id/members/:user_id removes member.
T167 — GET    /api/v1/orgs/:id/members lists members under RLS.
T169 — GET    /api/v1/orgs/:id/drivers/:uid/profile coach view.
T171 — GET    /api/v1/orgs/:id/dashboard coach roster.
T174 — GET    /api/v1/orgs/:id/metrics aggregate KPIs.
T175 — PATCH  /api/v1/orgs/:id/branding admin updates branding.
T176 — DELETE /api/v1/orgs/:id deletes org with cascade.

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
  - validates members + pending invites < seats_limit (T170:
    402 Payment Required with detail dict carrying
    {code, seats_limit, seats_used})
  - persists an OrganizationInvitation with a 256-bit random
    token (secrets.token_urlsafe(32)) and 7-day TTL
  - returns 201 with the full invitation shape including token
    (T165 redeems by token; email delivery is a later task)

T165 endpoint:
  - looks up the invitation by (org_id, token); 404 on miss
    (status parity to avoid token enumeration)
  - rejects expired (410), already-accepted (409), email
    mismatch (403)
  - inserts an OrganizationMember row with the invitation's
    role and stamps invitation.accepted_at = now() in the
    same transaction
  - returns 201 with OrganizationMembership shape

T166 endpoint:
  - validates the caller is the org admin (403, status parity
    on unknown-org)
  - rejects admin self-removal (409 Conflict — preserves the
    'every org has exactly one admin' invariant; admin
    departure goes through a future transfer / delete-org
    flow, not member removal)
  - returns 404 if the (org_id, user_id) pair has no member
    row
  - deletes the OrganizationMember row, returns 204
  - invitations are not touched on remove (audit history;
    a removed member could be re-invited later)

T167 endpoint:
  - app-layer RLS over OrganizationMember rows
  - 403 if caller is not a member of the org (status parity
    with unknown-org to avoid org-existence leakage)
  - role-based filtering: admin sees all roles; coach and
    driver see only role=driver entries
  - rationale: coaches work with drivers (their cohort is
    visible) but visibility into peer coaches and the org-
    billing-admin is admin-only. Drivers see their cohort
    peers under the same rule.

T169 endpoint:
  - 401 unauthenticated
  - 403 caller not in org (status parity with unknown-org)
  - 403 caller role is `driver` (drivers don't peer-spy via
    this endpoint — their /api/v1/driver/profile self-view
    serves their own data; peer-driver intel-gathering is
    out of scope)
  - 403 target user is not a `driver`-role member of the org
    (coaches don't view admin / peer-coach profiles — the
    endpoint is the coach<->driver supervision surface; admin
    and peer-coach visibility is admin-list-only via T167)
  - 200 returns DriverProfileAggregate (same shape as T004's
    self-view) — admin / coach in the org sees the driver's
    progress + weaknesses + prescriptions + readiness

The schema-layer Pydantic enforces input bounds (name 1-120,
seats_limit >= 1, plan ∈ {free, team, pro}, invite role ∈
{coach, driver}, email shape sanity-checked) so the endpoint
body deliberately holds no validation logic — the boundary is
the schema, not the controller.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

import uuid6
from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import (
    CoachDashboardDriverEntry,
    CoachDashboardResponse,
    DriverProfileAggregate,
    OrganizationBrandingUpdate,
    OrganizationCreate,
    OrganizationInvitationCreate,
    OrgMetrics,
)
from lap_schemas.v1 import Organization as OrganizationSchema
from lap_schemas.v1 import OrganizationInvitation as OrgInvitationSchema
from lap_schemas.v1 import (
    OrganizationMemberEntry as OrgMemberEntrySchema,
)
from lap_schemas.v1 import (
    OrganizationMemberList as OrgMemberListSchema,
)
from lap_schemas.v1 import (
    OrganizationMembership as OrgMembershipSchema,
)
from lap_schemas.v1.skill_drilldown import SkillDimensionName
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.error_codes import ErrorCode
from app.routers.driver_profile import build_driver_profile_aggregate
from infra.orm import ActionPrescription as ActionPrescriptionRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import DriverProgressSnapshot as ProgressSnapshotRow
from infra.orm import Organization as OrganizationRow
from infra.orm import OrganizationInvitation as OrgInvitationRow
from infra.orm import OrganizationMember as OrganizationMemberRow
from infra.orm import Session as SessionRow
from infra.orm import User as UserRow
from infra.orm import WeaknessEpisode as WeaknessEpisodeRow
from infra.stripe import update_subscription_quantity

router = APIRouter(prefix="/api/v1/orgs", tags=["organizations"])

_INVITE_TTL_DAYS = 7


async def _sync_org_seats_to_stripe(
    db: AsyncSession, org: OrganizationRow
) -> None:
    """T168 — keep Stripe's subscription quantity in sync with
    actual seat count. No-op for free-tier orgs (no sub id).

    Counts members AFTER the local commit, so the seat count
    reflects post-mutation state."""
    if org.stripe_subscription_id is None:
        return
    count = (
        await db.execute(
            select(func.count(OrganizationMemberRow.id)).where(
                OrganizationMemberRow.org_id == org.id
            )
        )
    ).scalar_one()
    update_subscription_quantity(
        org.stripe_subscription_id, int(count)
    )


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
        branding_logo_url=org.branding_logo_url,
    )


@router.patch(
    "/{org_id}/branding",
    response_model=OrganizationSchema,
    responses={
        403: {"description": "not_org_admin"},
    },
)
async def patch_organization_branding(
    org_id: UUID,
    body: OrganizationBrandingUpdate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrganizationSchema:
    """T175 — admin updates org branding metadata.

    Only the org admin can edit branding. Non-admin members
    (coach, driver) and outsiders all 403 with status parity
    on unknown-org.
    """
    org = (
        await db.execute(
            select(OrganizationRow).where(
                OrganizationRow.id == org_id
            )
        )
    ).scalar_one_or_none()
    if org is None or org.admin_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.NOT_ORG_ADMIN,
        )
    org.branding_logo_url = body.branding_logo_url
    await db.commit()
    await db.refresh(org)
    return OrganizationSchema(
        id=org.id,
        name=org.name,
        plan=org.plan,
        admin_user_id=org.admin_user_id,
        seats_limit=org.seats_limit,
        created_at=org.created_at,
        branding_logo_url=org.branding_logo_url,
    )


@router.post(
    "/{org_id}/invites",
    response_model=OrgInvitationSchema,
    status_code=status.HTTP_201_CREATED,
    responses={
        402: {"description": "seats_exhausted"},
        403: {"description": "not_org_admin"},
        409: {"description": "already_member"},
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
            detail=ErrorCode.NOT_ORG_ADMIN,
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
                detail=ErrorCode.ALREADY_MEMBER,
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
    seats_used = member_count + pending_invite_count
    if seats_used >= org.seats_limit:
        # T170 — 402 Payment Required (not 409). Semantically
        # this is a plan-tier limit, not a state conflict; the
        # client's CTA is "upgrade your plan", not "retry".
        # Detail dict carries the seat usage so the UI can
        # render "X/Y seats" without a second round-trip.
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "seats_exhausted",
                "seats_limit": org.seats_limit,
                "seats_used": seats_used,
            },
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


@router.post(
    "/{org_id}/invites/{token}/accept",
    response_model=OrgMembershipSchema,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "email_mismatch"},
        404: {"description": "invitation_not_found"},
        409: {"description": "already_accepted_or_member"},
        410: {"description": "invitation_expired"},
    },
)
async def accept_organization_invitation(
    org_id: UUID,
    token: str,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrgMembershipSchema:
    invitation = (
        await db.execute(
            select(OrgInvitationRow).where(
                OrgInvitationRow.org_id == org_id,
                OrgInvitationRow.token == token,
            )
        )
    ).scalar_one_or_none()
    if invitation is None:
        # Status parity: unknown org and unknown / mismatched
        # token both surface as 404. Opaque tokens already
        # prevent enumeration; defense-in-depth on response
        # uniformity.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.INVITATION_NOT_FOUND,
        )
    if invitation.accepted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ErrorCode.ALREADY_ACCEPTED,
        )
    now = datetime.now(UTC)
    expires_at = invitation.expires_at
    # SQLite drops tz info on round-trip; normalize so the
    # comparison doesn't TypeError on offset-naive vs aware.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=ErrorCode.INVITATION_EXPIRED,
        )
    if user.email != invitation.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.EMAIL_MISMATCH,
        )

    member = OrganizationMemberRow(
        id=uuid6.uuid7(),
        org_id=org_id,
        user_id=user.id,
        role=invitation.role,
    )
    db.add(member)
    invitation.accepted_at = now
    await db.commit()
    await db.refresh(member)
    # T168 — sync Stripe seat count after the local commit so
    # billing follows the actual member count.
    org = (
        await db.execute(
            select(OrganizationRow).where(
                OrganizationRow.id == org_id
            )
        )
    ).scalar_one()
    await _sync_org_seats_to_stripe(db, org)
    return OrgMembershipSchema(
        id=member.id,
        org_id=member.org_id,
        user_id=member.user_id,
        role=member.role,
        created_at=member.created_at,
    )


@router.delete(
    "/{org_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "not_org_admin"},
        404: {"description": "member_not_found"},
        409: {"description": "cannot_remove_admin"},
    },
)
async def remove_organization_member(
    org_id: UUID,
    user_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    org = (
        await db.execute(
            select(OrganizationRow).where(
                OrganizationRow.id == org_id
            )
        )
    ).scalar_one_or_none()
    if org is None or org.admin_user_id != user.id:
        # Status parity with T164: non-existent and not-admin
        # both return 403 to non-admins.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.NOT_ORG_ADMIN,
        )
    if user_id == org.admin_user_id:
        # Admin self-removal would orphan the org; admin
        # departure is a separate (future) flow.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ErrorCode.CANNOT_REMOVE_ADMIN,
        )
    member = (
        await db.execute(
            select(OrganizationMemberRow).where(
                OrganizationMemberRow.org_id == org_id,
                OrganizationMemberRow.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.MEMBER_NOT_FOUND,
        )
    await db.delete(member)
    await db.commit()
    # T168 — sync Stripe seat count after the local commit so
    # billing follows the actual member count (proration credits
    # the freed seat).
    await _sync_org_seats_to_stripe(db, org)


@router.get(
    "/{org_id}/members",
    response_model=OrgMemberListSchema,
    responses={
        403: {"description": "not_org_member"},
    },
)
async def list_organization_members(
    org_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrgMemberListSchema:
    # Caller-membership lookup doubles as the existence check:
    # if the org doesn't exist or the caller isn't a member,
    # the lookup returns None and we 403 (status parity).
    caller_membership = (
        await db.execute(
            select(OrganizationMemberRow).where(
                OrganizationMemberRow.org_id == org_id,
                OrganizationMemberRow.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if caller_membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.NOT_ORG_MEMBER,
        )

    stmt = select(OrganizationMemberRow).where(
        OrganizationMemberRow.org_id == org_id
    )
    if caller_membership.role != "admin":
        # RLS: coach/driver see only drivers — admin and peer
        # coaches are admin-visible only.
        stmt = stmt.where(OrganizationMemberRow.role == "driver")
    rows = (await db.execute(stmt)).scalars().all()
    entries = tuple(
        OrgMemberEntrySchema(
            id=r.id,
            org_id=r.org_id,
            user_id=r.user_id,
            role=r.role,
            created_at=r.created_at,
        )
        for r in rows
    )
    return OrgMemberListSchema(entries=entries)


@router.get(
    "/{org_id}/drivers/{user_id}/profile",
    response_model=DriverProfileAggregate,
    responses={
        403: {"description": "not_authorized"},
    },
)
async def view_org_driver_profile(
    org_id: UUID,
    user_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DriverProfileAggregate:
    """T169 — coach / admin views a driver's profile aggregate.

    Authz:
      - caller must be admin or coach in the org
      - target must be role=driver in the same org
      - all unauthorized cases return 403 (status parity to
        prevent existence leaks)
    """
    caller_membership = (
        await db.execute(
            select(OrganizationMemberRow).where(
                OrganizationMemberRow.org_id == org_id,
                OrganizationMemberRow.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if caller_membership is None or caller_membership.role == "driver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.NOT_AUTHORIZED,
        )
    target_membership = (
        await db.execute(
            select(OrganizationMemberRow).where(
                OrganizationMemberRow.org_id == org_id,
                OrganizationMemberRow.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if (
        target_membership is None
        or target_membership.role != "driver"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.NOT_AUTHORIZED,
        )
    return await build_driver_profile_aggregate(db, user_id)


# T171 — open-status enums match the existing per-route conventions
# (skills.py for weaknesses, driver_profile.py for prescriptions).
# Duplicated here rather than imported to avoid cross-router
# coupling for what is effectively a domain constant.
_OPEN_WEAKNESS_STATUSES = ("open", "improving")
_OPEN_RX_STATUSES = ("proposed", "accepted", "in_progress")


@router.get(
    "/{org_id}/dashboard",
    response_model=CoachDashboardResponse,
    responses={
        403: {"description": "not_authorized"},
    },
)
async def get_coach_dashboard(
    org_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CoachDashboardResponse:
    """T171 — coach / admin lists per-driver progress entries.

    Authz: caller must be admin or coach in the org. Drivers and
    non-members both 403 (status parity).

    The entries cover every driver-role member of the org, even
    those without a DriverProfile row yet (nullable identity +
    zero counts).
    """
    caller_membership = (
        await db.execute(
            select(OrganizationMemberRow).where(
                OrganizationMemberRow.org_id == org_id,
                OrganizationMemberRow.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if caller_membership is None or caller_membership.role == "driver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.NOT_AUTHORIZED,
        )

    driver_user_ids = list(
        (
            await db.execute(
                select(OrganizationMemberRow.user_id).where(
                    OrganizationMemberRow.org_id == org_id,
                    OrganizationMemberRow.role == "driver",
                )
            )
        ).scalars()
    )

    entries = tuple(
        await _build_dashboard_entries(db, driver_user_ids)
    )
    return CoachDashboardResponse(entries=entries)


async def _build_dashboard_entries(
    db: AsyncSession, target_user_ids: list[UUID]
) -> list[CoachDashboardDriverEntry]:
    """T190 — bulk-load every per-driver child table in a single
    query so the dashboard issues a fixed number of queries
    (4 + 2 setup) regardless of driver count. Pre-T190 this was
    `_build_dashboard_entry` called in a loop, producing 4N
    queries (a textbook N+1).

    A driver may not have a DriverProfile yet (org member who
    hasn't bootstrapped); in that case the row carries only the
    user_id with everything else nullable / zero. Consistent
    with the schema's defaults.
    """
    if not target_user_ids:
        return []

    # 1. Bulk-load profiles keyed by user_id.
    profile_rows = list(
        (
            await db.execute(
                select(DriverProfileRow).where(
                    DriverProfileRow.user_id.in_(target_user_ids)
                )
            )
        ).scalars()
    )
    profile_by_user: dict[UUID, DriverProfileRow] = {
        p.user_id: p for p in profile_rows
    }
    profile_ids = [p.id for p in profile_rows]

    # 2. Latest snapshot per driver. Loading all snapshots for
    # these drivers and dedup'ing in Python keeps the query
    # SQLite/Postgres-portable (DISTINCT ON is Postgres-only and
    # window functions are awkward across both dialects).
    latest_snap_by_driver: dict[UUID, ProgressSnapshotRow] = {}
    if profile_ids:
        snap_rows = (
            await db.execute(
                select(ProgressSnapshotRow)
                .where(
                    ProgressSnapshotRow.driver_id.in_(profile_ids)
                )
                .order_by(
                    ProgressSnapshotRow.captured_at.desc()
                )
            )
        ).scalars()
        for snap in snap_rows:
            if snap.driver_id not in latest_snap_by_driver:
                latest_snap_by_driver[snap.driver_id] = snap

    # 3. Open-weakness count per driver (single GROUP BY query).
    weakness_count_by_driver: dict[UUID, int] = {}
    if profile_ids:
        for did, count in (
            await db.execute(
                select(
                    WeaknessEpisodeRow.driver_id,
                    func.count(WeaknessEpisodeRow.id),
                )
                .where(
                    WeaknessEpisodeRow.driver_id.in_(profile_ids),
                    WeaknessEpisodeRow.status.in_(
                        _OPEN_WEAKNESS_STATUSES
                    ),
                )
                .group_by(WeaknessEpisodeRow.driver_id)
            )
        ).all():
            weakness_count_by_driver[did] = int(count)

    # 4. Open-prescription count per driver (single GROUP BY query).
    rx_count_by_driver: dict[UUID, int] = {}
    if profile_ids:
        for did, count in (
            await db.execute(
                select(
                    ActionPrescriptionRow.driver_id,
                    func.count(ActionPrescriptionRow.id),
                )
                .where(
                    ActionPrescriptionRow.driver_id.in_(profile_ids),
                    ActionPrescriptionRow.status.in_(
                        _OPEN_RX_STATUSES
                    ),
                )
                .group_by(ActionPrescriptionRow.driver_id)
            )
        ).all():
            rx_count_by_driver[did] = int(count)

    entries: list[CoachDashboardDriverEntry] = []
    for uid in target_user_ids:
        profile = profile_by_user.get(uid)
        if profile is None:
            entries.append(CoachDashboardDriverEntry(user_id=uid))
            continue
        latest = latest_snap_by_driver.get(profile.id)
        entries.append(
            CoachDashboardDriverEntry(
                user_id=uid,
                display_name=profile.display_name,
                skill_level=profile.skill_level,
                overall_score=(
                    latest.overall_score if latest else None
                ),
                last_progress_at=(
                    latest.captured_at if latest else None
                ),
                open_weaknesses=weakness_count_by_driver.get(
                    profile.id, 0
                ),
                open_prescriptions=rx_count_by_driver.get(
                    profile.id, 0
                ),
            )
        )
    return entries


# T174 — KPI windows match the global /admin/kpis convention
# (last 7 days for WPD), with a wider 30-day window for skill
# gain comparisons (a single week is too noisy).
_WPD_WINDOW = timedelta(days=7)
_GAIN_WINDOW = timedelta(days=30)
_DIM_NAMES: tuple[SkillDimensionName, ...] = (
    "braking",
    "throttle_pickup",
    "minimum_speed",
    "line_consistency",
    "tire_management",
    "racecraft",
    "fatigue_consistency",
    "strategy_execution",
    "setup_sensitivity",
    "adaptation",
)


@router.get(
    "/{org_id}/metrics",
    response_model=OrgMetrics,
    responses={
        403: {"description": "not_authorized"},
    },
)
async def get_org_metrics(
    org_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrgMetrics:
    """T174 — admin/coach reads org-level aggregate KPIs.

    Two metrics:
      - wpd: distinct drivers in this org with ≥1 session in
        the last 7 days
      - best_gain_dimension + best_gain_value: across the org's
        drivers, the SkillDimensionName whose `score` grew most
        on average over the last 30 days. None when fewer than
        2 snapshots exist for any driver (no delta).
    """
    caller_membership = (
        await db.execute(
            select(OrganizationMemberRow).where(
                OrganizationMemberRow.org_id == org_id,
                OrganizationMemberRow.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if (
        caller_membership is None
        or caller_membership.role == "driver"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.NOT_AUTHORIZED,
        )

    driver_user_ids = list(
        (
            await db.execute(
                select(OrganizationMemberRow.user_id).where(
                    OrganizationMemberRow.org_id == org_id,
                    OrganizationMemberRow.role == "driver",
                )
            )
        ).scalars()
    )
    if not driver_user_ids:
        return OrgMetrics(wpd=0)

    driver_profile_ids = list(
        (
            await db.execute(
                select(DriverProfileRow.id).where(
                    DriverProfileRow.user_id.in_(driver_user_ids)
                )
            )
        ).scalars()
    )
    if not driver_profile_ids:
        return OrgMetrics(wpd=0)

    now = datetime.now(UTC)
    wpd = (
        await db.execute(
            select(
                func.count(func.distinct(SessionRow.driver_id))
            ).where(
                SessionRow.driver_id.in_(driver_profile_ids),
                SessionRow.created_at >= now - _WPD_WINDOW,
            )
        )
    ).scalar_one()

    # Best gain: per-driver delta (latest score - earliest score
    # within the 30d window) per dimension, then averaged across
    # drivers per dimension. Drivers with <2 snapshots in window
    # contribute nothing for any dim.
    snap_rows = (
        (
            await db.execute(
                select(ProgressSnapshotRow)
                .where(
                    ProgressSnapshotRow.driver_id.in_(
                        driver_profile_ids
                    ),
                    ProgressSnapshotRow.captured_at
                    >= now - _GAIN_WINDOW,
                )
                .order_by(ProgressSnapshotRow.captured_at.asc())
            )
        )
        .scalars()
        .all()
    )
    best_dim, best_val = _compute_best_gain(snap_rows)
    return OrgMetrics(
        wpd=int(wpd),
        best_gain_dimension=best_dim,
        best_gain_value=best_val,
    )


def _compute_best_gain(
    snap_rows: Sequence[ProgressSnapshotRow],
) -> tuple[SkillDimensionName | None, float | None]:
    """Picks the dimension with the largest org-wide average
    score delta. Returns (None, None) when no driver has ≥2
    snapshots with a usable score for any dimension."""
    by_driver: dict[UUID, list[ProgressSnapshotRow]] = {}
    for r in snap_rows:
        by_driver.setdefault(r.driver_id, []).append(r)

    dim_deltas: dict[str, list[float]] = {d: [] for d in _DIM_NAMES}
    for snaps in by_driver.values():
        if len(snaps) < 2:
            continue
        # snap_rows is already sorted captured_at asc — first
        # is earliest, last is latest within the 30-day window
        earliest, latest = snaps[0], snaps[-1]
        for dim in _DIM_NAMES:
            early_dim = getattr(earliest, dim) or {}
            late_dim = getattr(latest, dim) or {}
            if "score" in early_dim and "score" in late_dim:
                delta = float(late_dim["score"]) - float(
                    early_dim["score"]
                )
                dim_deltas[dim].append(delta)

    avg_per_dim: dict[str, float] = {
        dim: sum(deltas) / len(deltas)
        for dim, deltas in dim_deltas.items()
        if deltas
    }
    if not avg_per_dim:
        return None, None
    best_dim = max(avg_per_dim, key=lambda d: avg_per_dim[d])
    return cast(SkillDimensionName, best_dim), avg_per_dim[best_dim]


@router.delete(
    "/{org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "not_org_admin"},
    },
)
async def delete_organization(
    org_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """T176 — admin permanently deletes the organization.

    Cascade (deleted with the org):
      - OrganizationMember rows for that org_id
      - OrganizationInvitation rows for that org_id

    Preserved (NOT org-owned):
      - User rows, DriverProfile rows, Session rows,
        CoachSessionNote rows — these are personal/session
        data that survives org churn

    Why explicit deletes (rather than relying on FK CASCADE)?
    Portable across SQLite (which has FK enforcement off by
    default) and Postgres. The schema-level CASCADE remains as
    defense-in-depth in production.

    Auth: admin only — coach / driver / outsider all 403,
    status parity with unknown-org.
    """
    org = (
        await db.execute(
            select(OrganizationRow).where(
                OrganizationRow.id == org_id
            )
        )
    ).scalar_one_or_none()
    if org is None or org.admin_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorCode.NOT_ORG_ADMIN,
        )
    # Tear down org-scoped rows explicitly so the deletion works
    # under SQLite (no FK enforcement) and stays self-documenting.
    await db.execute(
        delete(OrgInvitationRow).where(
            OrgInvitationRow.org_id == org_id
        )
    )
    await db.execute(
        delete(OrganizationMemberRow).where(
            OrganizationMemberRow.org_id == org_id
        )
    )
    await db.delete(org)
    await db.commit()
