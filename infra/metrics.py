"""Custom Prometheus metrics catalog (T176+).

Defined as module-level singletons against the default
prometheus_client registry, so importing this module is enough to
make every metric appear at /metrics. Routers and workers that
want to record events call into the constants here — no factory
needed.

Metric names follow Prometheus' SUFFIX conventions: `_total` for
monotonically-increasing counters, `_seconds` for durations,
`_bytes` for sizes. Labels stay low-cardinality (no per-user IDs)
so the time-series count doesn't explode.
"""

from prometheus_client import Counter, Gauge, Histogram

sessions_ingested_total = Counter(
    "sessions_ingested_total",
    "Number of telemetry sessions accepted by /sessions/ingest",
)

# Default buckets are generic (1ms..10s); the ingest path is mostly
# disk + DB, so 50ms..30s spread better matches what we'll actually
# observe in the wild.
ingest_duration_seconds = Histogram(
    "ingest_duration_seconds",
    "Wall-clock latency of /sessions/ingest from request entry to dispatch",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# T177 — coaching-domain metrics. Counter for prescription creation
# (monotonic — every drill generated bumps it). Gauge for open
# weakness episodes (in-flight count: +1 on open, -1 on resolve).
prescriptions_created_total = Counter(
    "prescriptions_created_total",
    "Number of ActionPrescription rows created via PrescriptionRepo",
)

weakness_episodes_open_total = Gauge(
    "weakness_episodes_open_total",
    "Number of WeaknessEpisode rows currently in the `open` state",
)

# T192 — business-metric histograms tied to the SLO budgets in
# docs/error-budget.md. Buckets bracket each SLO so a p95 quantile
# over budget is visible in the dashboard.
#
# coaching_pipeline_duration_seconds: post-session coaching SLO
# is 60s end-to-end (ingest accept → insights persisted). Buckets
# span 1s..120s so the slow tail is observable.
coaching_pipeline_duration_seconds = Histogram(
    "coaching_pipeline_duration_seconds",
    (
        "End-to-end latency of the post-session coaching pipeline"
        " (session ingest accept → coaching insights persisted)"
    ),
    buckets=(1.0, 2.5, 5.0, 10.0, 25.0, 60.0, 120.0, 300.0),
)

# next_session_plan_duration_seconds: next-session plan SLO is 5s
# (deterministic rules + cached ML predictions). Buckets span
# 50ms..10s so both the fast path and the slow tail are bucketed.
next_session_plan_duration_seconds = Histogram(
    "next_session_plan_duration_seconds",
    (
        "Latency of the next-session-plan compose step served by"
        " /api/v1/sessions/{id}/next_actions"
    ),
    buckets=(0.05, 0.1, 0.2, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# T193 — HTTP request counter feeding the SLO alert rule in
# deploy/prometheus/alerts.yml. Labels are deliberately MINIMAL
# (method + status_class) so cardinality stays bounded:
# 7 verbs × 5 classes ≤ 35 active series. Labeling by raw path
# would explode the count (every UUID-bearing route a fresh
# series); the alert rule sums across labels anyway, so per-route
# breakdown buys nothing for paging and a lot for storage cost.
#
# `status_class` is the leading digit of the HTTP status suffixed
# with "xx" — "2xx", "4xx", "5xx", etc. The middleware in
# `app.http_metrics.record_http_metrics` populates this on every
# response and on unhandled exceptions (counted as 5xx before
# re-raise — see that module's docstring for the ordering rationale).
http_requests_total = Counter(
    "http_requests_total",
    (
        "Total HTTP requests handled by the API, labeled by method"
        " and status class. Backs the 5xx-rate SLO alert in"
        " deploy/prometheus/alerts.yml."
    ),
    ["method", "status_class"],
)
