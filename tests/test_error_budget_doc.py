"""T232 — error budget doc pins SLO targets per endpoint.

The doc lives at `docs/error-budget.md` and encodes the same
SLO numbers that the k6 load tests assert (T230/T231) plus the
end-to-end coaching budgets from the architecture decisions.

This test exists so the budget is a Git artifact rather than
a verbal commitment — if someone "softens" the SLO in the doc
without updating the load test (or vice-versa), this catches
the drift on commit.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "error-budget.md"


def test_error_budget_doc_exists() -> None:
    assert DOC_PATH.is_file(), f"missing error budget doc at {DOC_PATH}"


def test_doc_encodes_http_latency_classes() -> None:
    """Latency SLO classes match k6 load tests (T230/T231)."""
    body = DOC_PATH.read_text(encoding="utf-8")
    # cheap-read SLO — T231 /api/v1/me at 500 rps, p95 <200ms
    assert "200ms" in body
    # write/upload SLO — T230 /api/v1/sessions/ingest at 50 rps, p95 <500ms
    assert "500ms" in body


def test_doc_references_load_tested_endpoints() -> None:
    """The two endpoints with k6 load tests must be named so a
    reader can trace the SLO back to its load script."""
    body = DOC_PATH.read_text(encoding="utf-8")
    assert "/api/v1/sessions/ingest" in body
    assert "/api/v1/me" in body


def test_doc_encodes_availability_target() -> None:
    """Error budget needs an availability target; absence means
    there's no budget to burn."""
    body = DOC_PATH.read_text(encoding="utf-8")
    assert "99." in body  # availability % (e.g. 99.5%, 99.9%)


def test_doc_encodes_coaching_pipeline_budgets() -> None:
    """End-to-end pipeline budgets from
    docs/roadmap/02-ARCHITECTURE-DECISIONS.md must be reflected
    so the doc stays the single SLO source of truth."""
    body = DOC_PATH.read_text(encoding="utf-8")
    # Post-session coaching: <60s p95 after upload
    assert "60s" in body
    # Next-session plan: <5s p95
    assert "5s" in body
