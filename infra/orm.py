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
    Index,
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
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    is_waitlist: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    trial_ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    failure_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
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
    # T008 (sprint2) — None = not flagged. First-flag-wins
    # idempotency lives in the router, not the schema.
    flagged_wrong_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("action_outcomes.id", ondelete="SET NULL"),
        nullable=True,
    )


class ActionOutcome(Base):
    __tablename__ = "action_outcomes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    prescription_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("action_prescriptions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_ids_measured: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    success_condition_met: Mapped[bool] = mapped_column(Boolean)
    measured_value: Mapped[float] = mapped_column(Float)
    delta_vs_baseline: Mapped[float] = mapped_column(Float)
    confidence_change: Mapped[float] = mapped_column(Float)
    prescription_efficacy_score: Mapped[float] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class DriverProgressSnapshot(Base):
    __tablename__ = "driver_progress_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window: Mapped[str] = mapped_column(String(16))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    braking: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    throttle_pickup: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    minimum_speed: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    line_consistency: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    tire_management: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    racecraft: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    fatigue_consistency: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    strategy_execution: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    setup_sensitivity: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    adaptation: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )

    # T073 endurance-weighted dimensions. Nullable because sprint-only
    # drivers won't have stint-level data; additive non-breaking
    # preserves every existing snapshot already in the DB.
    long_run_consistency: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    pace_decay: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    tire_preservation: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )

    overall_score: Mapped[float] = mapped_column(Float)
    overall_confidence: Mapped[float] = mapped_column(Float)
    percentile_vs_cohort: Mapped[float | None] = mapped_column(Float, nullable=True)
    generated_by: Mapped[str] = mapped_column(String(64))


class TransferLink(Base):
    __tablename__ = "transfer_links"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(8))
    session_pair: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    link_reason: Mapped[str] = mapped_column(String(32))
    shape_match_score: Mapped[float] = mapped_column(Float)
    intensity_match_score: Mapped[float] = mapped_column(Float)
    divergence_points: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    transferred_skills: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    non_transferred_skills: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    generated_report_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("coaching_insights.id", ondelete="SET NULL"),
        nullable=True,
    )
    # T023 (sprint2-backend) — driver feedback. Nullable until
    # POST /api/v1/bridge/links/:id/rate is called. Re-rating
    # overwrites in place; only one rating per link.
    usefulness_rating: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )
    rated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # T029 (sprint2-backend) — link lifecycle. Set when a session
    # in `session_pair` is deleted. Once stamped, never overwritten;
    # the missing session isn't coming back.
    stale_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # T025 (sprint2-backend) — GIN index over the JSONB
    # divergence_points array. jsonb_path_ops is half the size of
    # the default jsonb_ops and is the right operator class for
    # our containment query pattern (e.g.
    # `divergence_points @> '[{"track_distance_m": 312.5}]'`).
    # SQLite ignores the postgresql_* kwargs; the migration
    # guards on dialect so the actual CREATE INDEX runs only on
    # Postgres.
    __table_args__ = (
        Index(
            "ix_transfer_links_divergence_points_gin",
            "divergence_points",
            postgresql_using="gin",
            postgresql_ops={"divergence_points": "jsonb_path_ops"},
        ),
    )


class DriverBenchmarkProfile(Base):
    __tablename__ = "driver_benchmark_profiles"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cohort_definition: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    cohort_size: Mapped[int] = mapped_column(Integer)
    per_track_car_percentiles: Mapped[dict[str, float]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    per_dimension_percentiles: Mapped[dict[str, float]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    archetype: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DriverReadinessState(Base):
    __tablename__ = "driver_readiness_states"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    target_event: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    readiness_score: Mapped[float] = mapped_column(Float)
    volume_input: Mapped[float] = mapped_column(Float)
    quality_input: Mapped[float] = mapped_column(Float)
    recency_input: Mapped[float] = mapped_column(Float)
    consistency_input: Mapped[float] = mapped_column(Float)
    unresolved_weaknesses_input: Mapped[float] = mapped_column(Float)
    familiarity_input: Mapped[float] = mapped_column(Float)
    event_pressure_input: Mapped[float] = mapped_column(Float)
    recovery_input: Mapped[float] = mapped_column(Float)
    limiting_factor: Mapped[str] = mapped_column(String(64))
    likely_gain_area: Mapped[str] = mapped_column(String(64))
    recommended_sessions_before_event: Mapped[int] = mapped_column(Integer)


class UploadQueueItem(Base):
    __tablename__ = "upload_queue_items"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(16))
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_http_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunks_sent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    chunks_total: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CoachingInsight(Base):
    __tablename__ = "coaching_insights"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rule_id: Mapped[str] = mapped_column(String(128))
    rule_version: Mapped[str] = mapped_column(String(32))
    observation: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    likely_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    corner_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suggested_prescription_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("action_prescriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    flagged_false_positive: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )


class TrackLayout(Base):
    __tablename__ = "track_layouts"
    __table_args__ = (
        UniqueConstraint(
            "source_provider",
            "name",
            "layout_name",
            name="uq_track_layouts_provider_name_layout",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    source_provider: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    layout_name: Mapped[str] = mapped_column(String(64))
    start_finish_lat: Mapped[float] = mapped_column(Float)
    start_finish_lon: Mapped[float] = mapped_column(Float)
    start_finish_heading_deg: Mapped[float] = mapped_column(Float)
    sector_checkpoints: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    corner_names: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    master_track_id: Mapped[UUID] = mapped_column(Uuid, index=True)


class CarModel(Base):
    __tablename__ = "car_models"
    __table_args__ = (
        UniqueConstraint(
            "source_provider", "name", name="uq_car_models_provider_name"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    source_provider: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    car_class: Mapped[str] = mapped_column(String(32), index=True)
    master_car_id: Mapped[UUID] = mapped_column(Uuid, index=True)


class TrackEquivalence(Base):
    __tablename__ = "track_equivalences"
    __table_args__ = (
        UniqueConstraint(
            "track_a_id", "track_b_id", name="uq_track_equivalences_pair"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    track_a_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("track_layouts.id", ondelete="CASCADE"), index=True
    )
    track_b_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("track_layouts.id", ondelete="CASCADE"), index=True
    )
    confidence: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CarEquivalence(Base):
    __tablename__ = "car_equivalences"
    __table_args__ = (
        UniqueConstraint(
            "car_a_id", "car_b_id", name="uq_car_equivalences_pair"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    car_a_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("car_models.id", ondelete="CASCADE"), index=True
    )
    car_b_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("car_models.id", ondelete="CASCADE"), index=True
    )
    confidence: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeviceSession(Base):
    __tablename__ = "device_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CompareCase(Base):
    """T005 — driver-saved A/B (or N-way) session comparison.

    `session_ids` is a JSON array of session UUIDs as strings —
    JSON not a join table because the cardinality is small (2-10
    per case) and the order matters for "session A vs session B".
    """

    __tablename__ = "compare_cases"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    session_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    rationale: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CohortMetric(Base):
    """T054 — anonymized cohort metric pool.

    Privacy-by-design: every row is just (car_id, track_id, metric,
    value, session_time). NO driver_id. The cohort percentile compute
    (T056+) reads this table for aggregate stats; because the rows
    have no link back to a driver, percentiles surface no per-driver
    data even under accidental over-fetch.

    Why no FK on car_id/track_id: the ID columns hold the *master*
    catalog IDs (master_car_id / master_track_id), which group
    equivalent provider-specific cars/tracks. Those are not primary
    keys of car_models/track_layouts (those tables key on the per-
    provider id), so an FK isn't possible without a normalization
    pass — and the lack of an FK does not weaken the privacy
    guarantee, only referential integrity for orphaned IDs.

    Composite index on (car_id, track_id, metric) supports the
    percentile query path (T057): SELECT value WHERE car=? AND
    track=? AND metric=?.
    """

    __tablename__ = "cohort_metrics"
    __table_args__ = (
        Index(
            "ix_cohort_metrics_car_track_metric",
            "car_id",
            "track_id",
            "metric",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    car_id: Mapped[UUID] = mapped_column(Uuid)
    track_id: Mapped[UUID] = mapped_column(Uuid)
    metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    session_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EnduranceEvent(Base):
    """T067 — endurance race weekend aggregate.

    Many sessions belong to one event via SessionManifest.endurance_event_id
    (T066). Stores the planned-run shape (duration, stint count) and the
    denormalized roster of participating drivers.

    driver_ids stored as JSON list[UUID] (string form for portability):
    the participant list is small (1-6 drivers/entry) and read-mostly.
    A join table would add a second migration + an extra query for every
    read; the read-side simplicity wins at this point in the sprint.
    T068+ keeps the list synced as sessions arrive.
    """

    __tablename__ = "endurance_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    event_name: Mapped[str] = mapped_column(String(255))
    total_duration_seconds: Mapped[float] = mapped_column(Float)
    stint_count: Mapped[int] = mapped_column(Integer)
    driver_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )


class StintRecord(Base):
    """T069 — per-stint persistence for endurance sessions.

    One row per stint of a session. T068 detects the stint boundaries
    (start_lap/end_lap/lap_count); T070+ derive the per-stint metrics
    (consistency, pace decay, tire preservation) that get stored here.
    T069 owns *storage* only, not derivation.

    Cascade delete from sessions: when a session is purged (GDPR
    right-to-erasure), its stints go too. The endurance event itself
    survives (it can outlive any single contributor).

    Unique on (session_id, stint_index): a re-fire of the stint-
    detection job must not silently double-write rows and skew the
    EnduranceEvent rollup.
    """

    __tablename__ = "stint_records"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "stint_index", name="uq_stint_records_session_index"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    stint_index: Mapped[int] = mapped_column(Integer)
    start_lap: Mapped[int] = mapped_column(Integer)
    end_lap: Mapped[int] = mapped_column(Integer)
    lap_count: Mapped[int] = mapped_column(Integer)
    tire_age_laps: Mapped[int] = mapped_column(Integer)
    fuel_used_liters: Mapped[float] = mapped_column(Float)
    lap_time_decay_seconds: Mapped[float] = mapped_column(Float)


class StrategyPlan(Base):
    """T096 — pre-race strategy intent for one session.

    The driver's locked-in plan: stint count, fuel budget, and
    intended pit-lap numbers. T097's StrategyEvent log carries
    the runtime timeline (plan/armed/pit/stint_end/fcy/sc); this
    row is the canonical "what was the pre-race intent" answer.

    UNIQUE on session_id: one canonical plan per session. A
    re-plan during the race is logged as a new StrategyEvent with
    kind=plan rather than a row mutation here, so the audit trail
    survives. Without the constraint, two plans on the same
    session would both claim "the truth" and downstream
    plan-vs-actual diffs would have to pick one arbitrarily.

    CASCADE delete from sessions: GDPR right-to-erasure on a
    session purges its plan with it.

    planned_pit_laps stored as JSON list (cross-dialect; SQLite
    tests + Postgres prod) — the column is read whole, not
    queried into, so JSONB's GIN advantage doesn't apply.

    No index beyond the FK index; the only read shape today is
    "load plan for session X" which the unique constraint already
    backs with an index.
    """

    __tablename__ = "strategy_plans"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    session_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    planned_stints: Mapped[int] = mapped_column(Integer)
    planned_fuel_liters: Mapped[float] = mapped_column(Float)
    planned_pit_laps: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
        server_default="[]",
    )
    # T102 — 0-100 plan-vs-actual execution score. Null until
    # the session's strategy data is finalized (intermediate
    # plan-only state has no actual yet, so no score).
    execution_score: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )


class StrategyEvent(Base):
    """T097 — append-only timeline for runtime strategy.

    Pairs with T096's StrategyPlan (the canonical pre-race
    intent): this table is the runtime story — when the plan
    was set, when the driver armed an alternate strategy, when
    each pit stop happened, when stints ended, when full course
    yellow / safety car periods began.

    `kind` is a free-text String(16) — the spec set ({plan,
    armed, pit, stint_end, fcy, sc}) is enforced in the Pydantic
    request schema landing with T099 so a useful 422 surfaces
    instead of a CHECK-constraint reject.

    `details` carries the kind-specific payload (JSON dict).
    Held loosely on purpose: the contents evolve faster than the
    table shape (e.g., "armed" payloads gain new fields as the
    UI exposes more strategy choices), and pulling each
    sub-shape into its own column would explode the table for
    no read benefit.

    NOT unique on (session_id, at_lap, kind): two FCYs can both
    start at lap 5 in chaotic conditions; two pit stops on the
    same lap is real (formation/false-start); the timeline must
    record both.

    Server-stamped `created_at` for tie-breaking same-lap events
    — appended-order resolves an "armed → pit" pair where both
    landed on lap 30.

    CASCADE delete from sessions: GDPR right-to-erasure.

    No additional indexes; the only read shape today is
    "load timeline for session X ordered by lap" which the
    session_id FK index supports.
    """

    __tablename__ = "strategy_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    at_lap: Mapped[int] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TargetEvent(Base):
    """T081 — driver-pinned upcoming sim event or real-life session.

    Field shapes follow ADR §5.7 verbatim. driver_id is added beyond
    the §5.7 BaseModel shape because each pinned target belongs to
    one driver — this is the persistence layer where the relationship
    lives. CASCADE on driver delete: GDPR right-to-erasure drops the
    pinned targets when the driver row is purged.

    track_id and car_id are nullable because real-life events may
    reference a track/car not yet in our catalog at pin time. ends_at
    is nullable for point-in-time events (single qualifier session).
    notes is nullable free-form context.

    competitive_weight is a 0-1 float; bound enforcement happens in
    the Pydantic request schema landing in T082 (DB-level CHECK would
    reject the request before it reaches the validator that produces
    a useful 422 error message).

    No indexes yet: read patterns materialize in T084 (per-driver
    list) and T089 (readiness recomputation lookups). Speculative
    indexes now would commit to a query shape the consumers haven't
    defined.
    """

    __tablename__ = "target_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    driver_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("driver_profiles.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    track_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("track_layouts.id", ondelete="SET NULL"), nullable=True
    )
    car_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("car_models.id", ondelete="SET NULL"), nullable=True
    )
    competitive_weight: Mapped[float] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # T094 — per-event idempotency for the 24h/1h email reminders.
    # List of window labels already dispatched (e.g. ["24h"] after
    # the 24-hour-out reminder fires). The hourly Celery beat
    # filters on this so re-runs and clock-skew "still in the
    # window" beats don't double-send.
    notifications_sent: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
        server_default="[]",
    )


class IRacingCalendarEntry(Base):
    """T085 — one row per scraped iRacing calendar entry.

    Populated weekly by infra.iracing_calendar.fetch_and_store
    (T087 cron). Covers both rotating series weeks and one-off
    special events; the `kind` column distinguishes them so T086's
    parser and downstream consumers can branch.

    `source_id` carries iRacing's own identifier (a stable string
    across re-fetches) and is unique-indexed so the scraper's upsert
    can match existing rows without scanning the whole table.

    track_name and car_class are stored as free text from iRacing;
    T086 will resolve track_name to TrackLayout via the catalog
    bridge. Storing the raw string here means a new track that
    isn't yet in our catalog still gets recorded — we don't drop
    rows just because the catalog hasn't caught up.

    fetched_at is server-stamped on each upsert (NOT taken from
    the payload) — distinguishes "when iRacing scheduled this" from
    "when we last saw this in the feed". Stale-data detection uses
    fetched_at vs the cron schedule.
    """

    __tablename__ = "iracing_calendar_entries"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    source_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True
    )
    series_name: Mapped[str] = mapped_column(String(255))
    event_name: Mapped[str] = mapped_column(String(255))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    track_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    car_class: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyTemplate(Base):
    """T107 — driver-scoped library of named StrategyPlan-shaped
    templates the user saves for reuse ("my Le Mans strategy").

    Distinct from T096's StrategyPlan: the plan is per-session
    (UNIQUE on session_id), templates are per-driver and not
    bound to any session — they're the source intent users copy
    from when seeding a new race plan. The shape is identical
    (planned_stints, planned_fuel_liters, planned_pit_laps) plus
    a `name` for identification.

    UNIQUE(driver_id, name): a user can't accumulate ambiguous
    duplicates of their named playbook. Per-driver, not global,
    so two users can each have a "Spa 24h" template.

    Cascade delete from driver_profiles: GDPR right-to-erasure
    on a driver purges their template library with them.

    planned_pit_laps as JSON list — cross-dialect (SQLite tests +
    Postgres prod), read whole rather than queried into.
    """

    __tablename__ = "strategy_templates"

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid6.uuid7
    )
    driver_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("driver_profiles.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    planned_stints: Mapped[int] = mapped_column(Integer)
    planned_fuel_liters: Mapped[float] = mapped_column(Float)
    planned_pit_laps: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
        server_default="[]",
    )

    __table_args__ = (
        UniqueConstraint(
            "driver_id", "name",
            name="uq_strategy_templates_driver_id_name",
        ),
    )


class Organization(Base):
    """T161 — org-scoped container for team / fleet / coaching-
    academy use cases. Owns members (T162), invitations (T164),
    and seat-cap enforcement (T164). Distinct from `User` /
    `DriverProfile` which are individual-scoped: orgs are
    multi-user shared workspaces with their own billing and
    member-role permissions.

    `admin_user_id` FK → users.id is the org admin (typically
    the creator). RBAC checks downstream rely on this being a
    real, present user — not just a typed UUID — so the FK is
    enforced at the schema layer. No cascade: deleting an admin
    user must fail loudly rather than silently orphan the org's
    members and billing record.
    """

    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid6.uuid7
    )
    name: Mapped[str] = mapped_column(String(120))
    plan: Mapped[str] = mapped_column(
        String(32), default="free", server_default="free"
    )
    admin_user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), index=True
    )
    seats_limit: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # T168 — points at the Stripe subscription billing this org's
    # seats. NULL = free tier (no Stripe sync); set = seat count
    # is synced to Stripe on member add / remove with proration.
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # T175 — admin-uploaded logo URL. Storage-only; the upload
    # path (CDN/S3/data uri) is the client's concern.
    branding_logo_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )


class OrganizationMember(Base):
    """T162 — join table between Organization (T161) and User
    with role information. A user can hold one role per org;
    re-roling goes through the role-update path (T164/T165),
    not via duplicate inserts.

    `role` is String(32) — the {admin, coach, driver} enum is
    enforced upstream at the endpoint layer, not via DB CHECK,
    consistent with the codebase convention for role/status
    columns (e.g., User.subscription_status).

    Cascades:
      - org_id ON DELETE CASCADE — deleting an org cleans up
        its membership rows; orphaned members would leak into
        team-membership queries.
      - user_id ON DELETE CASCADE — GDPR right-to-erasure on a
        user purges their org memberships with them.
    """

    __tablename__ = "organization_members"

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid6.uuid7
    )
    org_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "org_id", "user_id",
            name="uq_organization_members_org_id_user_id",
        ),
    )


class OrganizationInvitation(Base):
    """T164 — pending invitation issued by an org admin to bring
    a new user (by email) into the org under a role.

    `token` is opaque, ~256-bit secret (secrets.token_urlsafe(32))
    used by T165's accept endpoint as the redemption credential.
    UNIQUE on token to prevent collisions.

    `expires_at` enforces 7-day TTL — invitations stale silently
    rather than living forever. Implicit status:
      - accepted_at IS NULL AND expires_at > now() → pending
      - accepted_at IS NOT NULL                      → accepted
      - accepted_at IS NULL AND expires_at <= now()  → expired

    Cascade on org_id: deleting an org cleans up its outstanding
    invitations along with members. No FK on email (the invitee
    might not have a user row yet — they're invited BY email).
    """

    __tablename__ = "organization_invitations"

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid6.uuid7
    )
    org_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(32))
    token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CoachSessionNote(Base):
    """T173 — free-text coach annotation on a driver's session.

    Authoring identity is the user who POSTed the note (recorded
    here, not in the session manifest). Cascade on session_id:
    deleting a session purges its notes. No FK constraint on
    author_user_id deletion — note history is preserved (NOT
    NULL because every note has a real author at write-time;
    if the user is later deleted, the row stays and the FK keeps
    pointing at the old id, which is then a dead reference. Use
    ON DELETE SET NULL to handle that case rather than CASCADE).

    No FK to Organization: a driver may be in multiple orgs, and
    the note's relevant context is the coach<->driver pairing
    enforced at the endpoint layer. Storing org_id here would
    over-constrain the model and complicate the authoring story
    when a driver is in two orgs and a coach in only one of
    them.
    """

    __tablename__ = "coach_session_notes"

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid6.uuid7
    )
    session_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
    )
    author_user_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AdminAuditLog(Base):
    """T197 — append-only audit log of mutating admin actions.

    Every state-changing admin endpoint writes one row here
    after its work commits. Rows answer "who did what to whom,
    when?" — the durable, queryable artifact regulators ask
    for (structlog lines are ephemeral and not joinable).

    `metadata_json` carries action-specific context — Stripe
    refund_id + amount for billing.refund, the new status
    string for billing.sync — pinned per-action so an auditor
    can cross-reference external systems (Stripe ledger) to
    our internal record.

    No FK cascade on admin_user_id: if the admin user is later
    deleted, the audit history must remain (can't undo what
    has been recorded). The FK has no explicit ondelete so
    the default NO ACTION blocks deletion if rows exist —
    consistent with "audit is forever". The audit-vs-erasure
    interaction with T195's user-delete is a future call.
    """

    __tablename__ = "admin_audit_log"

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid6.uuid7
    )
    admin_user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), index=True
    )
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[UUID | None] = mapped_column(
        Uuid, nullable=True, index=True
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
