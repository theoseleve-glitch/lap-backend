"""T002 (sprint2-backend) — opaque, tamper-resistant cursor format.

The cursor is a base64-encoded string that carries:
- `last_seen_at`  — the created_at of the last row the client saw,
  so the server can WHERE created_at < last_seen_at on resume.
- `sha256(user_id + last_seen_at)` integrity tag — proves the
  cursor was issued for this caller. Forging another user's
  cursor or tampering with the timestamp is detected and the
  server treats the cursor as absent (start from the top).

Why "opaque + stable":
- opaque: clients can't compute or guess valid cursors → the
  pagination contract is "echo what the server gave you, nothing
  more". Server is free to change encoding without breaking clients.
- stable: same (user_id, last_seen_at) → same cursor string.
  Important so deterministic test fixtures and idempotent client
  retries produce the same wire bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import uuid6

from app.cursor import decode_cursor, encode_cursor


def _user() -> UUID:
    return uuid6.uuid7()


def _ts() -> datetime:
    return datetime(2026, 4, 26, 14, 30, 0, tzinfo=UTC)


# -- round trip ----------------------------------------------------


def test_round_trip_returns_original_timestamp() -> None:
    user = _user()
    ts = _ts()
    cursor = encode_cursor(user, ts)
    assert decode_cursor(user, cursor) == ts


def test_same_inputs_produce_identical_cursor() -> None:
    """Stability: encoding is deterministic. Same (user, ts) →
    same string. Means clients can compare cursors safely and
    test fixtures stay reproducible."""
    user = _user()
    ts = _ts()
    assert encode_cursor(user, ts) == encode_cursor(user, ts)


def test_different_users_produce_different_cursors() -> None:
    """Even with the same timestamp, the cursor differs because
    the integrity tag binds to user_id."""
    ts = _ts()
    a = encode_cursor(_user(), ts)
    b = encode_cursor(_user(), ts)
    assert a != b


# -- tamper resistance ---------------------------------------------


def test_decode_with_wrong_user_returns_none() -> None:
    """A cursor minted for user A must not work for user B —
    otherwise B could replay A's cursor and learn pagination
    state."""
    user_a = _user()
    user_b = _user()
    cursor = encode_cursor(user_a, _ts())
    assert decode_cursor(user_b, cursor) is None


def test_decode_garbage_string_returns_none() -> None:
    assert decode_cursor(_user(), "not-a-valid-cursor!!!") is None


def test_decode_empty_string_returns_none() -> None:
    """Empty cursor = "no cursor" (first page). Must not raise."""
    assert decode_cursor(_user(), "") is None


def test_decode_random_base64_returns_none() -> None:
    """Arbitrary base64 that decodes cleanly but isn't a real
    cursor must still be rejected, not parsed permissively."""
    import base64
    fake = base64.urlsafe_b64encode(b"hello world").decode()
    assert decode_cursor(_user(), fake) is None


def test_tampered_timestamp_in_cursor_returns_none() -> None:
    """If a client extracts the timestamp portion and modifies
    it, the integrity tag no longer matches and the cursor is
    rejected — preventing pagination-state forgery."""
    import base64
    user = _user()
    valid = encode_cursor(user, _ts())
    decoded = base64.urlsafe_b64decode(valid.encode()).decode()
    # Replace the timestamp component while leaving the sig.
    ts_part, _, sig_part = decoded.rpartition("|")
    tampered_payload = f"2099-01-01T00:00:00+00:00|{sig_part}"
    tampered = base64.urlsafe_b64encode(tampered_payload.encode()).decode()
    assert decode_cursor(user, tampered) is None


# -- shape ---------------------------------------------------------


def test_cursor_is_url_safe_base64() -> None:
    """No `+` or `/` chars (use urlsafe alphabet so cursors can
    appear in query strings without percent-encoding gymnastics)."""
    cursor = encode_cursor(_user(), _ts())
    assert "+" not in cursor
    assert "/" not in cursor


def test_cursor_decodes_with_aware_datetime() -> None:
    """Resumed timestamp must be tz-aware so SQLAlchemy comparisons
    against created_at (which is timezone=True) work without raising."""
    user = _user()
    decoded = decode_cursor(user, encode_cursor(user, _ts()))
    assert decoded is not None
    assert decoded.tzinfo is not None
