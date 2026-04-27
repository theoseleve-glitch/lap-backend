import io
import json
import zipfile
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

import structlog
import uuid6
from fastapi import APIRouter, Depends, HTTPException, Response, status
from lap_schemas.v1 import (
    DriverProfile as DriverProfileSchema,
)
from lap_schemas.v1 import (
    DriverProfileUpdate,
)
from lap_schemas.v1 import (
    User as UserSchema,
)
from lap_schemas.v1.driver_profile import SkillLevel
from lap_schemas.v1.user import SubscriptionStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.flags import is_enabled
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import Session as SessionRow
from infra.orm import User as UserRow

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1", tags=["me"])


def _serialize(profile: DriverProfileRow) -> DriverProfileSchema:
    return DriverProfileSchema(
        id=profile.id,
        user_id=profile.user_id,
        display_name=profile.display_name,
        skill_level=cast(SkillLevel, profile.skill_level),
        cohort_opt_in=profile.cohort_opt_in,
    )


@router.get("/me", response_model=UserSchema)
async def read_me(
    user: Annotated[UserRow, Depends(get_current_user)],
) -> UserSchema:
    # T237 — `verbose_me_read` flag gates extra observability for
    # rollout debugging. Off by default; the response shape is
    # unaffected either way (flags must not change the wire format).
    if is_enabled("verbose_me_read"):
        logger.info(
            "me.read.verbose",
            clerk_user_id=user.clerk_user_id,
            user_id=str(user.id),
            subscription_status=user.subscription_status,
        )
    return UserSchema(
        id=user.id,
        clerk_user_id=user.clerk_user_id,
        email=user.email,
        created_at=user.created_at,
        subscription_status=cast(SubscriptionStatus, user.subscription_status),
        is_admin=user.is_admin,
    )


@router.patch("/me", response_model=DriverProfileSchema)
async def patch_me(
    update: DriverProfileUpdate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DriverProfileSchema:
    profile = (
        await db.execute(
            select(DriverProfileRow).where(DriverProfileRow.user_id == user.id)
        )
    ).scalar_one_or_none()

    if profile is None:
        if update.display_name is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="display_name required to create profile",
            )
        profile = DriverProfileRow(
            id=uuid6.uuid7(),
            user_id=user.id,
            display_name=update.display_name,
            skill_level=update.skill_level or "beginner",
            cohort_opt_in=(
                update.cohort_opt_in
                if update.cohort_opt_in is not None
                else False
            ),
        )
        db.add(profile)
    else:
        if update.display_name is not None:
            profile.display_name = update.display_name
        if update.skill_level is not None:
            profile.skill_level = update.skill_level
        if update.cohort_opt_in is not None:
            profile.cohort_opt_in = update.cohort_opt_in

    await db.commit()
    return _serialize(profile)


# T194 — GDPR Article 20 right-to-data-portability export. The
# response is a ZIP with JSON entries because that's the
# "structured, commonly used, machine-readable format" the
# regulation calls out. Internal flags (is_admin, is_waitlist,
# stripe_customer_id) are deliberately redacted — they're
# operational state, not data the subject is entitled to.
def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"unserializable type: {type(value)!r}")


def _user_export_dict(user: UserRow) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "clerk_user_id": user.clerk_user_id,
        "email": user.email,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "subscription_status": user.subscription_status,
    }


def _profile_export_dict(profile: DriverProfileRow) -> dict[str, Any]:
    return {
        "id": str(profile.id),
        "user_id": str(profile.user_id),
        "display_name": profile.display_name,
        "skill_level": profile.skill_level,
        "cohort_opt_in": profile.cohort_opt_in,
    }


def _session_export_dict(s: SessionRow) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "driver_id": str(s.driver_id),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "status": s.status,
        "manifest": s.manifest,
    }


@router.post("/me/export")
async def export_me(
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    profile = (
        await db.execute(
            select(DriverProfileRow).where(DriverProfileRow.user_id == user.id)
        )
    ).scalar_one_or_none()

    sessions: list[SessionRow] = []
    if profile is not None:
        sessions = list(
            (
                await db.execute(
                    select(SessionRow).where(SessionRow.driver_id == profile.id)
                )
            )
            .scalars()
            .all()
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "user.json",
            json.dumps(
                _user_export_dict(user), indent=2, default=_json_default
            ),
        )
        if profile is not None:
            zf.writestr(
                "driver_profile.json",
                json.dumps(
                    _profile_export_dict(profile),
                    indent=2,
                    default=_json_default,
                ),
            )
        zf.writestr(
            "sessions.json",
            json.dumps(
                [_session_export_dict(s) for s in sessions],
                indent=2,
                default=_json_default,
            ),
        )

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="lap-coach-export-{user.id}.zip"'
            ),
        },
    )
