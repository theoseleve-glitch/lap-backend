"""T239 — CHANGELOG.md follows Keep a Changelog 1.1, has M1 entries.

The changelog is the canonical "what shipped when" — not git log,
not the sprint file. Pinning the format here means a future edit
that drops the format, removes the [Unreleased] header, or
ships an empty M1 section breaks at commit, before downstream
consumers (release notes, version-pinning clients) trust stale
content.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"


def _body() -> str:
    return CHANGELOG_PATH.read_text(encoding="utf-8")


def test_changelog_file_exists() -> None:
    assert CHANGELOG_PATH.is_file(), (
        f"missing changelog at {CHANGELOG_PATH}"
    )


def test_changelog_references_keep_a_changelog_format() -> None:
    """A reference link to keepachangelog.com proves the format
    was chosen deliberately. Without it, future "let's just write
    free-form release notes" edits look like cleanup, not regression."""
    body = _body()
    assert "keepachangelog.com" in body.lower()


def test_changelog_has_unreleased_section() -> None:
    """[Unreleased] is the staging area where in-flight changes
    accumulate before the next version cuts. A changelog without
    it forces every PR to also bump versions."""
    body = _body()
    assert "## [Unreleased]" in body


def test_changelog_has_m1_release_section() -> None:
    """M1 is the milestone this 30-day sprint represents. The
    section must be present and labeled with the M1 marker so
    release notes can mechanically derive what shipped."""
    body = _body()
    assert "M1" in body


def test_changelog_uses_canonical_subsections() -> None:
    """Keep a Changelog defines six canonical subsection labels:
    Added, Changed, Deprecated, Removed, Fixed, Security. At
    least Added must be present (M1 ships new behavior, by
    definition)."""
    body = _body()
    assert "### Added" in body


def test_changelog_m1_section_is_not_empty() -> None:
    """An M1 section header with no body underneath would tick
    the format box without conveying any release content. Pin
    that the M1 release header is followed by at least one bullet
    before the next `## ` section starts."""
    body = _body()
    # Split into chunks delimited by H2 headers; find the chunk
    # whose header line contains "M1" — that's the release section.
    sections = body.split("\n## ")
    m1_chunks = [
        s
        for s in sections
        if s.splitlines()[0:1] and "M1" in s.splitlines()[0]
    ]
    assert m1_chunks, "no `## ...M1...` section header found"
    bullet_lines = [
        line
        for line in m1_chunks[0].splitlines()
        if line.lstrip().startswith("-")
    ]
    assert bullet_lines, "M1 release section has no bullet entries"
