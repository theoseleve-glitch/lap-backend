"""T234 — security scans (pip-audit + bandit) wired into CI.

Both tools must run on every PR and fail the build on
high-severity findings. The tests pin the workflow shape so a
future edit that drops a scan, lowers the severity threshold,
or accidentally allows broad ignores fails at commit.

Why both:
- pip-audit catches CVEs in the dependency graph (data-driven,
  changes weekly as new advisories are published).
- bandit catches insecure code patterns we wrote ourselves
  (static analysis on our source).

Neither tool is sufficient alone — a CVE-clean app can still
contain hardcoded secrets, and clean source can still pull a
vulnerable dep. They cover different threat models.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _ci() -> dict:
    return yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))


def _all_steps() -> list[dict]:
    cfg = _ci()
    steps: list[dict] = []
    for job in cfg["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def test_ci_workflow_exists() -> None:
    assert CI_PATH.is_file()


def test_ci_runs_pip_audit() -> None:
    """pip-audit must run on every CI invocation. Look for the
    binary name in any step's `run:` script."""
    runs = " ".join(s.get("run", "") for s in _all_steps())
    assert "pip-audit" in runs


def test_ci_runs_bandit() -> None:
    runs = " ".join(s.get("run", "") for s in _all_steps())
    assert "bandit" in runs


def test_bandit_fails_on_medium_or_higher() -> None:
    """`-ll` = report level low-or-higher reporting threshold,
    BUT bandit's exit code rule is: nonzero exit if any issue at
    or above the SEVERITY threshold (`-l`/`-ll`/`-lll`). We use
    `-ll` (medium+) so low-severity advisories don't break CI
    while still gating on real findings.

    If a future edit weakens to `-lll` (high only) or removes
    the flag entirely (default = no severity gate), the build
    silently passes vulnerable code. This test stops that."""
    runs = " ".join(s.get("run", "") for s in _all_steps())
    # `bandit ... -ll` or `bandit ... --severity-level medium`
    assert "-ll" in runs or "severity-level medium" in runs


def test_pip_audit_skips_editable_only() -> None:
    """Editable installs (lap-schemas sibling repo) cannot be
    audited from a sibling repo's CI run. We skip those but must
    NOT skip the rest. Any broader skip is a hole."""
    runs = " ".join(s.get("run", "") for s in _all_steps())
    assert "--skip-editable" in runs
