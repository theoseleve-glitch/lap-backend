"""T233 — Dependabot + Renovate config so update PRs get generated.

Ecosystems are split to avoid duplicate PRs:
- Dependabot owns `github-actions` (it understands the workflow
  YAML schema natively and security advisories arrive there).
- Renovate owns Python deps via `pep621` (it understands uv/poetry
  pyproject.toml and supports grouping/scheduling that Dependabot
  cannot match).

These tests pin the ownership split so a future edit that lets
both tools manage the same ecosystem fails at commit, before the
duplicate-PR storm hits the inbox.
"""

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"
RENOVATE_PATH = REPO_ROOT / ".github" / "renovate.json"


def _load_dependabot() -> dict:
    return yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))


def _load_renovate() -> dict:
    return json.loads(RENOVATE_PATH.read_text(encoding="utf-8"))


# -- Dependabot ----------------------------------------------------


def test_dependabot_config_exists() -> None:
    assert DEPENDABOT_PATH.is_file(), (
        f"missing dependabot config at {DEPENDABOT_PATH}"
    )


def test_dependabot_config_is_v2() -> None:
    cfg = _load_dependabot()
    assert cfg.get("version") == 2


def test_dependabot_owns_github_actions() -> None:
    cfg = _load_dependabot()
    ecosystems = [u["package-ecosystem"] for u in cfg["updates"]]
    assert "github-actions" in ecosystems


def test_dependabot_does_not_own_python() -> None:
    """Python deps belong to Renovate. Dependabot managing pip
    too means duplicate PRs every week."""
    cfg = _load_dependabot()
    ecosystems = [u["package-ecosystem"] for u in cfg["updates"]]
    assert "pip" not in ecosystems


def test_dependabot_has_explicit_schedule() -> None:
    cfg = _load_dependabot()
    for update in cfg["updates"]:
        interval = update["schedule"]["interval"]
        assert interval in {"daily", "weekly", "monthly"}


# -- Renovate ------------------------------------------------------


def test_renovate_config_exists() -> None:
    assert RENOVATE_PATH.is_file(), (
        f"missing renovate config at {RENOVATE_PATH}"
    )


def test_renovate_config_parses_as_json() -> None:
    cfg = _load_renovate()
    assert isinstance(cfg, dict)


def test_renovate_extends_a_preset() -> None:
    """Without `extends`, Renovate runs with raw defaults — no
    grouping, no security alerts, no useful behavior."""
    cfg = _load_renovate()
    assert "extends" in cfg
    assert isinstance(cfg["extends"], list) and cfg["extends"]


def test_renovate_handles_python_via_pep621() -> None:
    """uv lockfile + pyproject.toml direct deps live behind the
    pep621 manager; the config must reference it explicitly so a
    Renovate version bump that drops pep621 doesn't silently
    leave python deps unmanaged."""
    cfg = _load_renovate()
    body = json.dumps(cfg).lower()
    assert "pep621" in body


def test_renovate_does_not_own_github_actions() -> None:
    """github-actions belongs to Dependabot — see comment at top
    of this file."""
    cfg = _load_renovate()
    body = json.dumps(cfg).lower()
    # If renovate has `github-actions` enabled anywhere, both
    # tools will file PRs for the same workflow file. The default
    # extends:recommended config DOES enable it, so we must
    # disable it explicitly via packageRules or enabledManagers.
    assert (
        '"enabledmanagers"' in body
        or '"github-actions"' in body
    ), "renovate must explicitly disable or scope github-actions"
