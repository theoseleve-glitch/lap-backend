"""T197 — admin audit log writer.

Single-writer module: every state-changing admin endpoint
imports `record_admin_action` from here, never instantiates
the ORM row directly. Centralizing the write site means a
single regression (e.g. someone misnaming `metadata_json` as
`metadata`) lands in one place rather than scattered across
endpoint files.

Usage at the call site (after the action's main DB writes
have committed, before the response is returned):

    from infra.admin_audit import record_admin_action
    ...
    await record_admin_action(
        db,
        admin_user_id=caller.id,
        action="billing.refund",
        target_id=charge_id_uuid_or_none,
        metadata={"refund_id": refund_id, "amount": amount},
    )
    await db.commit()

`action` strings follow `<area>.<verb>` so downstream
tooling can pivot on the prefix (e.g. all `billing.*`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import uuid6
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import AdminAuditLog


async def record_admin_action(
    db: AsyncSession,
    *,
    admin_user_id: UUID,
    action: str,
    target_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one row to admin_audit_log. Caller is responsible
    for committing — typically the same transaction as the
    action being recorded so audit + state stay consistent."""
    db.add(
        AdminAuditLog(
            id=uuid6.uuid7(),
            admin_user_id=admin_user_id,
            action=action,
            target_id=target_id,
            metadata_json=metadata,
        )
    )
