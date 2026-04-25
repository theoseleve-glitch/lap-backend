"""T112 fix ADR-005 missing FKs

Adds the two foreign-key constraints that the original create_table migrations
omitted (T065 action_prescriptions.outcome_id, T068 transfer_links.generated_report_id).
ADR-005 declares both columns as typed UUID7 references to specific entities;
without the FK, orphan and garbage-pointer writes are possible. Surfaced at the
ADR-005 schema-checkpoint review on 2026-04-25 before routers expose these tables.

Revision ID: c1f0a4b8d2e7
Revises: e91dd402c8aa
Create Date: 2026-04-25 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f0a4b8d2e7"
down_revision: str | Sequence[str] | None = "e91dd402c8aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Uses batch_alter_table so the migration runs on both PostgreSQL (direct
    ALTER TABLE ADD CONSTRAINT) and SQLite (copy-and-move recreate strategy).
    """
    with op.batch_alter_table("action_prescriptions") as batch_op:
        batch_op.create_foreign_key(
            "fk_action_prescriptions_outcome_id_action_outcomes",
            "action_outcomes",
            ["outcome_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("transfer_links") as batch_op:
        batch_op.create_foreign_key(
            "fk_transfer_links_generated_report_id_coaching_insights",
            "coaching_insights",
            ["generated_report_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("transfer_links") as batch_op:
        batch_op.drop_constraint(
            "fk_transfer_links_generated_report_id_coaching_insights",
            type_="foreignkey",
        )
    with op.batch_alter_table("action_prescriptions") as batch_op:
        batch_op.drop_constraint(
            "fk_action_prescriptions_outcome_id_action_outcomes",
            type_="foreignkey",
        )
