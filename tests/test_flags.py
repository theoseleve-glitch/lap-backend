"""T237 — env-backed feature flags.

The flag system is intentionally minimal: a function `is_enabled`
that reads `FLAG_<UPPER_NAME>` from the environment and returns
True for canonical truthy values, False otherwise.

Why env vars and not a database table:
- Toggling a flag does not require a deploy (env var changes
  re-read on each call) but ALSO does not require live DB writes
  during traffic — failure modes are bounded to "the new variant
  doesn't start" rather than "all of prod queries the flag table
  on every request".
- Per-environment rollout is the natural unit (staging on,
  prod off) and env vars already encode environment.
- Adding a third backend later (LaunchDarkly, custom DB) is a
  same-signature swap behind `is_enabled`.
"""

from __future__ import annotations

import pytest

from app.flags import is_enabled

# -- truthy / falsy matrix -----------------------------------------


@pytest.mark.parametrize(
    "value",
    ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON"],
)
def test_truthy_values_enable_flag(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("FLAG_NEW_FEATURE", value)
    assert is_enabled("new_feature") is True


@pytest.mark.parametrize(
    "value",
    ["0", "false", "FALSE", "no", "off", "", "maybe", "2", "null"],
)
def test_non_truthy_values_disable_flag(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Strict allowlist: anything outside the canonical truthy set
    is disabled. Avoids the classic "FLAG_X=disabled" trap where
    a thoughtful operator's intent silently turns the feature on
    because Python `bool('disabled')` is True."""
    monkeypatch.setenv("FLAG_NEW_FEATURE", value)
    assert is_enabled("new_feature") is False


def test_unset_flag_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-off rollout: a flag that hasn't been deployed to
    this env is implicitly disabled, not an error."""
    monkeypatch.delenv("FLAG_UNSET_FEATURE", raising=False)
    assert is_enabled("unset_feature") is False


# -- naming convention --------------------------------------------


def test_flag_name_uppercased_with_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The convention is FLAG_<UPPER_NAME> regardless of how the
    caller spells it. Pinning so a future "let's allow custom
    prefixes" edit can't silently change semantics for existing
    flags already deployed to staging/prod."""
    monkeypatch.setenv("FLAG_MIXED_CASE", "1")
    # All of these resolve to the same env var.
    assert is_enabled("mixed_case") is True
    assert is_enabled("MIXED_CASE") is True
    assert is_enabled("Mixed_Case") is True


def test_different_flags_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLAG_A", "1")
    monkeypatch.delenv("FLAG_B", raising=False)
    assert is_enabled("a") is True
    assert is_enabled("b") is False
