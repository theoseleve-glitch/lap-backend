from typing import Any

import uuid6
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import User


class _EmailAddress(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    email_address: str


class _UserData(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    primary_email_address_id: str | None = None
    email_addresses: list[_EmailAddress] = []


class _ClerkEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    data: dict[str, Any]


class InvalidClerkEventError(ValueError):
    """Raised when a Clerk webhook payload cannot be interpreted."""


def parse_event(payload: dict[str, Any]) -> _ClerkEvent:
    try:
        return _ClerkEvent.model_validate(payload)
    except ValidationError as exc:
        raise InvalidClerkEventError("malformed event envelope") from exc


def _primary_email(data: _UserData) -> str:
    if not data.email_addresses:
        raise InvalidClerkEventError("user.created event has no email_addresses")
    if data.primary_email_address_id is not None:
        for ea in data.email_addresses:
            if ea.id == data.primary_email_address_id:
                return ea.email_address
    return data.email_addresses[0].email_address


async def apply_clerk_user_event(
    db: AsyncSession, event: _ClerkEvent
) -> None:
    if event.type == "user.created":
        try:
            data = _UserData.model_validate(event.data)
        except ValidationError as exc:
            raise InvalidClerkEventError("user.created data invalid") from exc
        email = _primary_email(data)
        existing = (
            await db.execute(
                select(User).where(User.clerk_user_id == data.id)
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id=data.id,
                    email=email,
                )
            )
            await db.commit()
        return
    if event.type == "user.deleted":
        clerk_id = event.data.get("id")
        if not isinstance(clerk_id, str) or not clerk_id:
            raise InvalidClerkEventError("user.deleted data missing id")
        await db.execute(
            delete(User).where(User.clerk_user_id == clerk_id)
        )
        await db.commit()
        return
