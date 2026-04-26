"""T230 — k6 load script for /sessions/ingest at 50 rps.

The actual load test runs in CI via the k6 binary
(`k6 run scripts/k6/sessions_ingest.js`); this Python test
verifies the script file exists in the repo and encodes the
sprint's performance contract: 50 rps, p95 <500ms, hits the
ingest endpoint.

Test exists so the perf budget lives in Git as code rather
than as a verbal commitment — if someone tweaks the script
to fudge the SLO, this catches it on commit.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "k6" / "sessions_ingest.js"


def test_k6_script_file_exists() -> None:
    assert SCRIPT_PATH.is_file(), (
        f"missing k6 script at {SCRIPT_PATH}"
    )


def test_k6_script_targets_ingest_endpoint() -> None:
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "/api/v1/sessions/ingest" in body


def test_k6_script_encodes_50_rps_target() -> None:
    """Sprint contract: 50 rps. Encoded via constant-arrival-rate
    executor at rate=50."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "rate: 50" in body
    assert "constant-arrival-rate" in body


def test_k6_script_encodes_p95_500ms_threshold() -> None:
    """Sprint contract: p95 < 500ms. Encoded as a k6 threshold
    so a regressing run fails the load test immediately rather
    than passing silently with degraded perf."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "p(95)<500" in body
