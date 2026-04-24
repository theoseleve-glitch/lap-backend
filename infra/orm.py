from datetime import datetime
from typing import Any
from uuid import UUID

import uuid6
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    clerk_user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    subscription_status: Mapped[str] = mapped_column(
        String(32), default="inactive", server_default="inactive"
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )


class DriverProfile(Base):
    __tablename__ = "driver_profiles"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    display_name: Mapped[str] = mapped_column(String(128))
    skill_level: Mapped[str] = mapped_column(
        String(32), default="beginner", server_default="beginner"
    )
    cohort_opt_in: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    # Stored as JSONB on Postgres (indexable/queryable) and plain JSON on SQLite.
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(
        String(32), default="uploaded", server_default="uploaded"
    )


class SessionRecord(Base):
    __tablename__ = "session_records"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), unique=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )


class TelemetryArtifact(Base):
    __tablename__ = "telemetry_artifacts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    s3_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)


class Lap(Base):
    __tablename__ = "laps"
    __table_args__ = (
        UniqueConstraint("session_id", "lap_number", name="uq_laps_session_lap"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    lap_number: Mapped[int] = mapped_column(Integer)
    time_ms: Mapped[int] = mapped_column(Integer)
    sector_times: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    valid: Mapped[bool] = mapped_column(Boolean)


class WeaknessEpisode(Base):
    __tablename__ = "weakness_episodes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(32))
    subcategory: Mapped[str] = mapped_column(String(128))
    corner_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment_scope: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recurrence_count: Mapped[int] = mapped_column(Integer)
    sessions_affected: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    severity: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    confidence_decay_per_day: Mapped[float] = mapped_column(
        Float, default=0.02, server_default="0.02"
    )
    status: Mapped[str] = mapped_column(String(16))
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    linked_prescriptions: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    linked_insights: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    library_rule_id: Mapped[str] = mapped_column(String(128))


class ActionPrescription(Base):
    __tablename__ = "action_prescriptions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    weakness_episode_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("weakness_episodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str] = mapped_column(String(8))
    due_before_session_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    title: Mapped[str] = mapped_column(String(256))
    rationale: Mapped[str] = mapped_column(Text)
    drill_template_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_context: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    success_condition: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    status: Mapped[str] = mapped_column(String(16))
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
