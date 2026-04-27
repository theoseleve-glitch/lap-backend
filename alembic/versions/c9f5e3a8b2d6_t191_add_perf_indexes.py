"""T191 add performance indexes for top-N slow queries

Revision ID: c9f5e3a8b2d6
Revises: b8e2c5d4f9a1
Create Date: 2026-04-27 14:45:00.000000

Adds 6 composite indexes covering the hottest WHERE/ORDER BY
predicates in the endpoint handlers. Each index targets a query
that's either run on every page load (driver dashboard, coach
dashboard) or scans a table that grows linearly with sessions /
prescriptions / weaknesses (i.e., the things drivers create
constantly).

Indexes added (all composite — single-column scans are already
covered by the existing FK indexes):

1. `weakness_episodes (driver_id, status)`
   Used by: driver-profile aggregate (top-weaknesses filter),
   coach dashboard (open-weaknesses count). Without this, the
   open-status filter scans every row for the driver_id partition.

2. `action_prescriptions (driver_id, status)`
   Used by: driver-profile aggregate (active-prescriptions),
   coach dashboard (open-prescriptions count), prescriptions
   list endpoint with status=open filter.

3. `driver_progress_snapshots (driver_id, captured_at)`
   Used by: driver-profile aggregate ("latest snapshot per
   driver" subquery), coach dashboard (latest_snap_by_driver).
   The composite makes the ORDER BY captured_at DESC step a
   straight backward index scan.

4. `sessions (driver_id, created_at)`
   Used by: driver session-list endpoint, session-detail
   look-back queries. driver_id alone is indexed but the
   ORDER BY created_at DESC step still requires a sort.

5. `organization_members (org_id, role)`
   Used by: list-org-drivers, coach dashboard (drivers only),
   list-org-members with role filter. Without this, the
   role filter scans every member of the org.

6. `organization_invitations (org_id, accepted_at, expires_at)`
   Used by: seat-budget computation in invite creation
   (pending-invites count where accepted_at IS NULL AND
   expires_at > now()). Three-column composite covers both
   filter columns at once.

All 6 indexes are CREATE INDEX (not unique). Naming follows the
Alembic auto-naming convention for portability.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "c9f5e3a8b2d6"
down_revision: str | Sequence[str] | None = "b8e2c5d4f9a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ix_weakness_episodes_driver_id_status",
        "weakness_episodes",
        ("driver_id", "status"),
    ),
    (
        "ix_action_prescriptions_driver_id_status",
        "action_prescriptions",
        ("driver_id", "status"),
    ),
    (
        "ix_progress_snapshots_driver_id_captured_at",
        "driver_progress_snapshots",
        ("driver_id", "captured_at"),
    ),
    (
        "ix_sessions_driver_id_created_at",
        "sessions",
        ("driver_id", "created_at"),
    ),
    (
        "ix_organization_members_org_id_role",
        "organization_members",
        ("org_id", "role"),
    ),
    (
        "ix_organization_invitations_org_accepted_expires",
        "organization_invitations",
        ("org_id", "accepted_at", "expires_at"),
    ),
)


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.create_index(name, table, list(cols))


def downgrade() -> None:
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
