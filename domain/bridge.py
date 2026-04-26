"""Bridge-link domain helpers.

Pure DB-level operations on transfer_links that aren't tied to a
specific HTTP endpoint. Routers call these; tests exercise them
directly without spinning up the FastAPI app.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import TransferLink as TransferLinkRow


async def mark_transfer_links_stale_for_session(
    db: AsyncSession, session_id: UUID
) -> int:
    """T029 — when a Session is deleted, stamp `stale_at` on every
    TransferLink whose `session_pair` contains that id.

    Returns the count of links *newly* marked stale. A link that
    was already stale (e.g., because its other paired session was
    deleted earlier) is not re-stamped — the first deletion is the
    truth, so downstream "stale within the last 7 days" filters
    stay correct.

    `session_pair` is JSONB on Postgres but SQLAlchemy's `?` /
    `@>` operators don't translate to SQLite (our test fixture).
    For sprint scope we scan in Python — typical drivers have
    well under a million links, and stale-marking happens once per
    session deletion. A future Postgres-only optimization could
    use `WHERE session_pair @> '["...uuid..."]'`.

    Caller commits — this function only stages.
    """
    target = str(session_id)
    rows = (
        (
            await db.execute(
                select(TransferLinkRow).where(
                    TransferLinkRow.stale_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    n = 0
    for row in rows:
        if target in row.session_pair:
            row.stale_at = now
            n += 1
    return n
