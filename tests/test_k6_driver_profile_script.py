"""T231 — k6 load script for driver profile read at 500 rps.

The sprint label "/driver/profile" maps to the existing
`GET /api/v1/me` endpoint — the cheap authenticated read for
driver-self data. No new route added; the load test exercises
the framework + auth + simple-read path at 500 rps with a
p95 < 200ms budget.

This Python test pins the perf contract terms in Git so any
script edit that fudges the SLO breaks at commit time.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "k6" / "driver_profile.js"


def test_k6_script_file_exists() -> None:
    assert SCRIPT_PATH.is_file(), (
        f"missing k6 script at {SCRIPT_PATH}"
    )


def test_k6_script_targets_me_endpoint() -> None:
    """Real route is /api/v1/me — the driver-self read."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "/api/v1/me" in body


def test_k6_script_encodes_500_rps_target() -> None:
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "rate: 500" in body
    assert "constant-arrival-rate" in body


def test_k6_script_encodes_p95_200ms_threshold() -> None:
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "p(95)<200" in body
