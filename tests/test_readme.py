"""T240 — README is onboarding-complete: a new dev gets running in 1h.

The success criterion is "a new dev could onboard in 1h" — too
qualitative to assert directly, but each load-bearing section
*can* be pinned. If any of these is missing, the README has
silently regressed past the onboarding bar.

Sections enforced:
- Quickstart with concrete dev commands (uv + uvicorn)
- Tests command
- Architecture reference (link to ADR doc + an inline diagram)
- Deployment / env-var overview
- Project layout pointer

Pinning the structure means a future "let's clean up the README"
edit that strips a section fails at commit, prompting a deliberate
decision about whether the bar moved or the section moved
elsewhere (CONTRIBUTING.md, etc).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"


def _body() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_readme_exists() -> None:
    assert README_PATH.is_file()


def test_readme_has_quickstart_with_uv_command() -> None:
    """A new dev needs to know which package manager to use and
    the exact command to run the API. Hardcoding `uv` here also
    prevents drift to a half-pip half-uv setup."""
    body = _body().lower()
    assert "quickstart" in body or "getting started" in body
    assert "uv sync" in body or "uv run" in body
    assert "uvicorn" in body


def test_readme_documents_test_command() -> None:
    body = _body()
    assert "pytest" in body or "make test" in body


def test_readme_links_to_architecture_decisions() -> None:
    """ADR doc is the architectural source of truth. README must
    point a new dev there rather than re-summarize."""
    body = _body()
    assert "02-ARCHITECTURE-DECISIONS" in body or "ADR" in body


def test_readme_has_architecture_diagram() -> None:
    """An inline Mermaid diagram renders on GitHub and lives in
    Git so it can't drift from a separate PNG. Mermaid is enough
    for the 1h-onboarding bar; UML-grade tooling would be overkill."""
    body = _body()
    assert "```mermaid" in body, (
        "no inline mermaid diagram — onboarding bar is "
        "'a new dev sees the boxes-and-arrows in the README'"
    )


def test_readme_has_deployment_or_env_section() -> None:
    """A new dev needs to know what env vars the app reads and
    where it deploys, even if the deploy itself is gated."""
    body = _body().lower()
    assert "deploy" in body or "environment variable" in body or "env var" in body


def test_readme_has_project_layout_pointer() -> None:
    """The dir convention (app/ routers, domain/ business logic,
    infra/ side-effects, coaching/ rules engine) is non-obvious
    on first encounter. A one-line "what lives where" prevents
    new endpoints from sprouting in the wrong package."""
    body = _body().lower()
    assert "layout" in body or "structure" in body or "domain/" in body


def test_readme_references_changelog() -> None:
    """T239 added CHANGELOG.md; the README is where a new dev
    discovers it. Without a pointer, the changelog is invisible."""
    body = _body()
    assert "CHANGELOG" in body
