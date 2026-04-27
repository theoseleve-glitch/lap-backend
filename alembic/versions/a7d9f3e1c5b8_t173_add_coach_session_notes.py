"""T173 add coach_session_notes

Revision ID: a7d9f3e1c5b8
Revises: f6c8d3a9b2e7
Create Date: 2026-04-27 12:00:00.000000

CoachSessionNote — free-text coach annotation attached to a
driver's session. The authoring identity is recorded server-
side from the JWT (not provided by the client).

Cascade on session_id: deleting a session purges its notes.
ON DELETE SET NULL on author_user_id: if the authoring user is
deleted (right-to-erasure / cleanup), the note row stays — the
review history is preserved even if the author identity is
purged. Body is plain TEXT (no length cap at the DB layer; the
schema-side cap is 5000 chars).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d9f3e1c5b8"
down_revision: str | Sequence[str] | None = "f6c8d3a9b2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coach_session_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_coach_session_notes_session_id",
        "coach_session_notes",
        ["session_id"],
    )
    op.create_index(
        "ix_coach_session_notes_author_user_id",
        "coach_session_notes",
        ["author_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_coach_session_notes_author_user_id",
        table_name="coach_session_notes",
    )
    op.drop_index(
        "ix_coach_session_notes_session_id",
        table_name="coach_session_notes",
    )
    op.drop_table("coach_session_notes")
