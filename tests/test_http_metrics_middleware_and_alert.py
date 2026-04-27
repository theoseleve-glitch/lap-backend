"""T193 (sprint2-backend) — alerting on sustained 5xx >0.5%.

The 99.5% availability SLO in `docs/error-budget.md` translates
to a 0.5% error budget. Without an HTTP-level request counter
labeled by status class, no Prometheus alert rule can phrase
"5xx rate over the last 5m exceeds 0.5%" — the metric simply
doesn't exist to query.

T193 splits into three pinned behaviors:

  1. `http_requests_total` is defined in `infra.metrics` with
     `method` and `status_class` labels and only those labels
     (path is *deliberately* not a label — UUID-bearing routes
     would explode the active series count).
  2. A middleware in `app.http_metrics` populates it on every
     response, AND on unhandled exceptions (counted as 5xx
     before re-raise — otherwise the very class we're alerting
     on goes uncounted because ServerErrorMiddleware sits above
     our middleware).
  3. An alert rule YAML at `deploy/prometheus/alerts.yml`
     phrases the SLO breach: 5xx ratio > 0.005 sustained for a
     `for:` window. Without this file, prod's Alertmanager has
     nothing to load — the runbook in `disaster-recovery.md`
     (which references PagerDuty escalation) has no trigger.

Each test below corresponds to one of those concrete
deliverables, so a regression in any single piece — the metric,
the middleware wiring, or the alert rule — fails this file.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.http_metrics import record_http_metrics
from app.main import app as main_app
from infra import metrics as metrics_module
from infra.metrics import http_requests_total

REPO_ROOT = Path(__file__).resolve().parents[1]
ALERTS_YAML = REPO_ROOT / "deploy" / "prometheus" / "alerts.yml"


def test_http_requests_total_metric_defined_in_infra_metrics() -> None:
    """T193 #1 — the counter exists. Without it, the alert rule
    has no series to query."""
    assert hasattr(metrics_module, "http_requests_total"), (
        "infra.metrics must define http_requests_total"
    )


def test_http_requests_total_label_set_is_method_and_status_class_only() -> None:
    """T193 #1 cardinality guard — labeling by raw path would
    create a new time series for every UUID-bearing URL. The
    alert rule sums across labels anyway, so we keep the label
    set minimal: method (7 verbs) × status_class (5 classes)
    bounds the active series at ≤35."""
    label_names = sorted(http_requests_total._labelnames)  # type: ignore[attr-defined]
    assert label_names == ["method", "status_class"], (
        f"unexpected label set {label_names}; "
        "must be method+status_class only for cardinality"
    )


def test_record_http_metrics_callable_importable() -> None:
    """T193 #2 — middleware function exists. The dispatch shape
    matches the `BaseHTTPMiddleware` contract used elsewhere in
    main.py (rate_limit_middleware in app.rate_limit)."""
    assert callable(record_http_metrics)


def test_middleware_increments_2xx_counter_on_success() -> None:
    """T193 #2 — happy path. A GET that returns 200 must bump
    http_requests_total{method="GET",status_class="2xx"}."""
    client = TestClient(main_app)
    before = http_requests_total.labels(
        method="GET", status_class="2xx"
    )._value.get()  # type: ignore[attr-defined]
    response = client.get("/healthz")
    assert response.status_code == 200
    after = http_requests_total.labels(
        method="GET", status_class="2xx"
    )._value.get()  # type: ignore[attr-defined]
    assert after == before + 1, (
        f"2xx counter did not increment: before={before} after={after}"
    )


def test_middleware_increments_4xx_counter_on_client_error() -> None:
    """T193 #2 — a 404 must land in the 4xx bucket, not 2xx and
    not 5xx. Distinguishing 4xx from 5xx is what makes the alert
    rule expressible at all."""
    client = TestClient(main_app)
    before = http_requests_total.labels(
        method="GET", status_class="4xx"
    )._value.get()  # type: ignore[attr-defined]
    response = client.get("/this-route-definitely-does-not-exist")
    assert response.status_code == 404
    after = http_requests_total.labels(
        method="GET", status_class="4xx"
    )._value.get()  # type: ignore[attr-defined]
    assert after == before + 1


def test_middleware_increments_5xx_counter_on_unhandled_exception() -> None:
    """T193 #2 — the alerting case. ServerErrorMiddleware sits
    OUTSIDE our middleware (it's the outermost Starlette layer),
    so when a handler raises an unhandled exception it propagates
    UP past us before being converted to a 500. We must catch
    it, count 5xx, and re-raise — otherwise the very thing we're
    alerting on never increments. This test mounts a one-off app
    (not main_app) so we don't poison main_app's route table."""
    test_app = FastAPI()
    test_app.add_middleware(
        BaseHTTPMiddleware,
        dispatch=record_http_metrics,
    )

    @test_app.get("/boom")
    async def _boom() -> dict[str, str]:  # pragma: no cover - intentional raise
        raise RuntimeError("boom")

    @test_app.get("/oops")
    async def _oops() -> dict[str, str]:
        # 500 returned via HTTPException — flows back through
        # call_next as a normal Response, NOT as an exception.
        # Test pins that this path also counts 5xx.
        raise HTTPException(status_code=500, detail="oops")

    before_boom = http_requests_total.labels(
        method="GET", status_class="5xx"
    )._value.get()  # type: ignore[attr-defined]
    client = TestClient(test_app, raise_server_exceptions=False)
    boom_resp = client.get("/boom")
    assert boom_resp.status_code == 500
    mid = http_requests_total.labels(
        method="GET", status_class="5xx"
    )._value.get()  # type: ignore[attr-defined]
    assert mid == before_boom + 1, (
        f"unhandled-exception 5xx not counted: {before_boom}->{mid}"
    )

    oops_resp = client.get("/oops")
    assert oops_resp.status_code == 500
    after_oops = http_requests_total.labels(
        method="GET", status_class="5xx"
    )._value.get()  # type: ignore[attr-defined]
    assert after_oops == mid + 1, (
        f"HTTPException(500) 5xx not counted: {mid}->{after_oops}"
    )


def test_main_app_has_http_metrics_middleware_registered() -> None:
    """T193 #2 wiring — defining the middleware function isn't
    enough; main.py must wire it. Detect via the dispatch
    callable: BaseHTTPMiddleware stores the dispatch in its
    options kwargs."""
    dispatch_callables = []
    for m in main_app.user_middleware:
        opts = getattr(m, "kwargs", {}) or {}
        if "dispatch" in opts:
            dispatch_callables.append(opts["dispatch"])
    assert record_http_metrics in dispatch_callables, (
        "record_http_metrics is defined but not wired in app.main; "
        "user_middleware kwargs.dispatch values: "
        f"{[c.__name__ if hasattr(c, '__name__') else c for c in dispatch_callables]}"
    )


def test_alert_rules_yaml_exists_and_loads() -> None:
    """T193 #3 — the file Alertmanager scrapes. Without it, the
    counter and middleware are inert: nothing fires."""
    assert ALERTS_YAML.is_file(), (
        f"missing alert rules at {ALERTS_YAML.relative_to(REPO_ROOT)}"
    )
    data = yaml.safe_load(ALERTS_YAML.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "groups" in data, (
        "alerts.yml must be a Prometheus rule-group document "
        "(top-level `groups:` key)"
    )


def _all_alert_rules() -> list[dict[str, object]]:
    data = yaml.safe_load(ALERTS_YAML.read_text(encoding="utf-8"))
    return [
        rule
        for group in data.get("groups", [])
        for rule in group.get("rules", [])
        if "alert" in rule  # filter recording-rules out
    ]


def test_5xx_alert_rule_present() -> None:
    """T193 #3 — at least one alert rule must reference the 5xx
    class. The rule's name is conventionally derived from the
    SLO target ('99.5%') — case-insensitive match on either
    '5xx' in the name or the expression."""
    rules = _all_alert_rules()
    matching = [
        r
        for r in rules
        if "5xx" in str(r.get("alert", "")).lower()
        or "5xx" in str(r.get("expr", ""))
    ]
    assert matching, (
        "no alert rule references 5xx; rules defined: "
        f"{[r.get('alert') for r in rules]}"
    )


def test_5xx_alert_threshold_is_zero_point_five_percent() -> None:
    """T193 #3 — the threshold must match the 99.5% SLO budget
    exactly. 0.005 = 0.5%. A common drift is to write 0.05
    (5%) or 0.5 (50%) — both would silently disable paging."""
    rules = _all_alert_rules()
    fivexx = next(
        r
        for r in rules
        if "5xx" in str(r.get("alert", "")).lower()
        or "5xx" in str(r.get("expr", ""))
    )
    expr = str(fivexx["expr"])
    assert "0.005" in expr, (
        f"5xx alert expr must contain 0.005 threshold (matches "
        f"99.5% SLO in docs/error-budget.md); got expr: {expr}"
    )


def test_5xx_alert_has_sustained_for_clause() -> None:
    """T193 #3 — 'sustained' in the task title means the alert
    must fire only after the breach has held for a window. A
    bare `expr` without `for:` would page on a single noisy
    minute. Per Prometheus docs, `for:` is the duration the
    expression has to stay true before pending → firing."""
    rules = _all_alert_rules()
    fivexx = next(
        r
        for r in rules
        if "5xx" in str(r.get("alert", "")).lower()
        or "5xx" in str(r.get("expr", ""))
    )
    assert "for" in fivexx, (
        "5xx alert must include a `for:` clause to require "
        "sustained breach; rule keys: "
        f"{list(fivexx.keys())}"
    )
    assert fivexx["for"], "`for:` value must be non-empty"


def test_5xx_alert_expr_uses_http_requests_total_ratio() -> None:
    """T193 #3 — the alert expression must compute a ratio
    against `http_requests_total`. Otherwise it's not measuring
    rate and not measuring our metric. Pinning this specific
    metric name catches a future rename of the Counter that
    would silently break alerting."""
    rules = _all_alert_rules()
    fivexx = next(
        r
        for r in rules
        if "5xx" in str(r.get("alert", "")).lower()
        or "5xx" in str(r.get("expr", ""))
    )
    expr = str(fivexx["expr"])
    assert "http_requests_total" in expr, (
        f"alert expr must reference http_requests_total; got: {expr}"
    )
    assert "rate(" in expr, (
        "alert expr must compute a rate (instantaneous "
        f"`http_requests_total` is a monotonic counter); got: {expr}"
    )
    assert "/" in expr, (
        f"alert expr must compute a ratio (5xx / total); got: {expr}"
    )
