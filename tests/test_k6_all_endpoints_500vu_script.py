"""T200 (sprint2-backend) — load test pass 2 structural pin.

The k6 script at scripts/k6/all_endpoints_500vu.js encodes the
sprint pass-2 contract: 500 concurrent VUs, p95 <300ms across
the cheap-read mix, error rate <1%. This Python test pins those
terms in Git so any later script edit that fudges the SLO breaks
at commit time, not at the next staging load run.

Pinning rationale (mirrors test_k6_driver_profile_script.py):
the actual run happens in a staging environment with real Clerk
bearer + the real backend, not in pytest. CI's job is to keep
the *contract* in the script honest; the *measurement* is run
out-of-band.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "k6" / "all_endpoints_500vu.js"


def test_k6_pass2_script_file_exists() -> None:
    assert SCRIPT_PATH.is_file(), f"missing k6 script at {SCRIPT_PATH}"


def test_k6_pass2_uses_constant_vus_executor() -> None:
    """Concurrent-users semantics → constant-vus, not the
    constant-arrival-rate used by per-endpoint throughput tests
    (T230/T231). Wrong executor measures throughput, not
    concurrency-induced contention — which is the whole point
    of pass 2."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "constant-vus" in body, (
        "pass-2 must use constant-vus to surface concurrency "
        "contention; offered-rate executors don't"
    )


def test_k6_pass2_encodes_500_concurrent_vus() -> None:
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "vus: 500" in body, (
        "pass-2 contract is 500 concurrent users; threshold "
        "must reflect it"
    )


def test_k6_pass2_encodes_p95_300ms_threshold() -> None:
    """The 300ms target is tighter than the 500ms heavy-read
    budget per docs/error-budget.md — pinning it means a
    regression in the cheap-read path can't hide behind the
    looser class budget."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "p(95)<300" in body


def test_k6_pass2_targets_cheap_class_endpoints() -> None:
    """The endpoint mix must be cheap-class only (per
    error-budget.md). Mixing in write/upload would dilute the
    300ms signal — those have their own tests with the
    looser 500ms budget."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    for path in (
        "/api/v1/me",
        "/api/v1/catalog/tracks",
        "/api/v1/coaching/rules",
    ):
        assert path in body, (
            f"expected {path} in the pass-2 endpoint mix"
        )


def test_k6_pass2_does_not_target_ingest_or_write_paths() -> None:
    """Counter-pin: the write/upload paths (which have their
    own tests at T230 with a 500ms budget) must NOT be in the
    300ms-bracketed mix. Including them would either: (a)
    inflate p95 above 300ms and false-fail the test, or (b)
    pass for the wrong reason if the inflated reads stay
    under 300ms while a slow ingest path drifts unnoticed.
    Either way the contract gets muddied."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "/api/v1/sessions/ingest" not in body, (
        "ingest path doesn't belong in the cheap-class mix"
    )


def test_k6_pass2_failure_rate_threshold() -> None:
    """A pass-2 run that's fast but errors at 50% is not a
    pass. Pin the <1% error-rate floor."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "rate<0.01" in body, (
        "pass-2 must fail the run on >1% errors, not just on "
        "p95 breach"
    )
