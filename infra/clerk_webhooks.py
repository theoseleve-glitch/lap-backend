import base64
import binascii
import hashlib
import hmac
import time
from typing import Any

import uuid6
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import User

# Reject signed payloads older than this many seconds. Svix recommends
# rejecting after ~5 minutes to mitigate replay attacks while tolerating
# small clock skew between Clerk and our backend.
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60


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


class InvalidSignatureError(Exception):
    """Raised when a Svix signature header fails verification."""


def _decode_secret(secret: str) -> bytes:
    if not secret:
        raise InvalidSignatureError("webhook secret not configured")
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_") :]
    try:
        return base64.b64decode(secret)
    except (ValueError, binascii.Error) as exc:
        raise InvalidSignatureError("malformed webhook secret") from exc


def verify_svix_signature(
    *,
    secret: str,
    svix_id: str | None,
    svix_timestamp: str | None,
    svix_signature: str | None,
    body: bytes,
    now: int | None = None,
    tolerance_seconds: int = WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
) -> None:
    """Verify a Svix-signed Clerk webhook.

    Raises InvalidSignatureError on any failure.
    """
    if not svix_id or not svix_timestamp or not svix_signature:
        raise InvalidSignatureError("missing svix headers")
    try:
        ts_int = int(svix_timestamp)
    except ValueError as exc:
        raise InvalidSignatureError("svix-timestamp not an integer") from exc
    current = now if now is not None else int(time.time())
    if abs(current - ts_int) > tolerance_seconds:
        raise InvalidSignatureError("svix-timestamp outside tolerance window")

    key = _decode_secret(secret)
    signed_content = f"{svix_id}.{svix_timestamp}.".encode() + body
    expected = base64.b64encode(
        hmac.new(key, signed_content, hashlib.sha256).digest()
    ).decode()

    for entry in svix_signature.split(" "):
        version, _, candidate = entry.partition(",")
        if version != "v1" or not candidate:
            continue
        if hmac.compare_digest(candidate, expected):
            return
    raise InvalidSignatureError("no matching signature")


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
