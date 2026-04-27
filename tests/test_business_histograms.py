"""T192 (sprint2-backend) — Prometheus custom histograms for
business metrics.

The coaching-pipeline and next-session-plan SLOs in
`docs/error-budget.md` are end-user-visible budgets (60s and 5s
respectively). Without histograms tracking the actual latency
distribution, we can't tell if we're burning the budget — we
only see the slow tail when it shows up as 5xx via Sentry.

T175 wired the default registry to `/metrics`. T176/T177 added
counters + a gauge. T192 adds two histograms tied to the SLO
budgets so prod can scrape them and dashboard them.

Pinned behaviors:
  - `coaching_pipeline_duration_seconds` is defined and visible
    at /metrics with the documented bucket boundaries
  - `next_session_plan_duration_seconds` is defined and visible
    at /metrics
  - Both metrics' bucket sets bracket their respective SLO so a
    p95 over budget is observable in the histogram quantile
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_coaching_pipeline_histogram_in_metrics_module() -> None:
    """The histogram must be defined as a module-level constant
    in infra.metrics so simply importing the module registers
    it on the default Prometheus registry."""
    from infra import metrics

    assert hasattr(metrics, "coaching_pipeline_duration_seconds"), (
        "infra.metrics must define coaching_pipeline_duration_seconds"
    )


def test_next_session_plan_histogram_in_metrics_module() -> None:
    from infra import metrics

    assert hasattr(metrics, "next_session_plan_duration_seconds"), (
        "infra.metrics must define next_session_plan_duration_seconds"
    )


def test_coaching_pipeline_histogram_buckets_bracket_60s_slo() -> None:
    """The post-session coaching pipeline budget is 60s
    (docs/error-budget.md). The histogram's bucket boundaries
    must span values both below and above that SLO so a p95
    greater than 60s is observable in the quantile estimate.
    Default Prometheus buckets (10ms..10s) wouldn't cover this."""
    from infra import metrics

    hist = metrics.coaching_pipeline_duration_seconds
    # prometheus_client exposes the bucket upper bounds via the
    # private _upper_bounds attribute. The +Inf bucket is added
    # automatically; we check the explicit list.
    bounds = list(hist._upper_bounds)  # type: ignore[attr-defined]
    # +Inf is always last; drop it for the "bracketed by" check.
    finite_bounds = [b for b in bounds if b != float("inf")]
    assert max(finite_bounds) >= 60.0, (
        f"max bucket {max(finite_bounds)} doesn't bracket 60s SLO; "
        f"buckets={finite_bounds}"
    )
    assert min(finite_bounds) <= 5.0, (
        "histogram needs sub-SLO buckets to capture the fast path"
    )


def test_next_session_plan_histogram_buckets_bracket_5s_slo() -> None:
    """SLO is 5s. Buckets must span both fast (<200ms) and slow
    (>5s) for the histogram to be useful at quantile time."""
    from infra import metrics

    hist = metrics.next_session_plan_duration_seconds
    bounds = list(hist._upper_bounds)  # type: ignore[attr-defined]
    finite_bounds = [b for b in bounds if b != float("inf")]
    assert max(finite_bounds) >= 5.0, (
        f"max bucket {max(finite_bounds)} doesn't bracket 5s SLO; "
        f"buckets={finite_bounds}"
    )
    assert min(finite_bounds) <= 0.2, (
        "histogram needs sub-200ms buckets for the fast path"
    )


def test_metrics_endpoint_exposes_business_histograms() -> None:
    """The /metrics endpoint scraping is the customer here. All
    custom metrics must appear in its body — defining them in
    infra.metrics is necessary but not sufficient if main.py
    fails to import the module."""
    # Import side-effect: ensure the module is loaded so the
    # metrics register before /metrics is scraped.
    import infra.metrics  # noqa: F401

    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert "coaching_pipeline_duration_seconds" in body, (
        "coaching_pipeline_duration_seconds not in /metrics scrape"
    )
    assert "next_session_plan_duration_seconds" in body, (
        "next_session_plan_duration_seconds not in /metrics scrape"
    )
