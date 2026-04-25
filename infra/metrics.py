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
