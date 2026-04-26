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
