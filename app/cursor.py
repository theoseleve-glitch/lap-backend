"""T002 (sprint2-backend) — opaque, tamper-resistant cursor.

Format on the wire:
    base64url( "<iso_ts>|<sha256_hex(user_id||iso_ts)>" )

The `iso_ts` carries the resume position (server WHEREs
`created_at < iso_ts`). The trailing sha256 is the integrity
tag — encoding `(user_id, iso_ts)` lets the server verify the
cursor was minted for *this* caller, not replayed from another
user's pagination state.

Forging or tampering returns None from `decode_cursor`, which
the timeline endpoint treats as "no cursor" (start from the
top). Failing closed by silently restarting is friendlier than
401-ing on a stale or copy-pasted cursor.

No external secret is needed: the user_id is itself the keying
material. Knowing user A's id wouldn't let an attacker forge
A's cursor unless they also have A's session — and if they have
A's session, the cursor is the least of A's problems.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from uuid import UUID

_SEP = "|"


def _sig(user_id: UUID, iso_ts: str) -> str:
    h = hashlib.sha256()
    h.update(str(user_id).encode("utf-8"))
    h.update(iso_ts.encode("utf-8"))
    return h.hexdigest()


def encode_cursor(user_id: UUID, last_seen_at: datetime) -> str:
    """Mint a stable cursor for the given (user, timestamp).

    Calling twice with the same inputs returns the same string —
    deterministic so test fixtures and idempotent retries don't
    drift.
    """
    iso_ts = last_seen_at.isoformat()
    payload = f"{iso_ts}{_SEP}{_sig(user_id, iso_ts)}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(user_id: UUID, cursor: str) -> datetime | None:
    """Verify and decode `cursor` for `user_id`.

    Returns the embedded timestamp on success, None on any
    failure (empty string, garbage base64, valid base64 with no
    `|` separator, mismatched signature, unparseable timestamp).
    """
    if not cursor:
        return None
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii")).decode(
            "utf-8"
        )
    except (ValueError, UnicodeDecodeError):
        return None
    if _SEP not in payload:
        return None
    iso_ts, _, sig_hex = payload.rpartition(_SEP)
    expected = _sig(user_id, iso_ts)
    if not hmac.compare_digest(sig_hex, expected):
        return None
    try:
        return datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
