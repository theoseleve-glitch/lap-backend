"""T074 (sprint2-backend) — endurance-specific coaching rules.

Authors a directory of YAML rules under
`coaching/library/rules/endurance/`. The library loader (T182)
validates each file against RuleSpec at startup; this test pins:

  - the directory loads without error (every YAML is well-formed
    and matches the schema)
  - at least five rules are present (acceptance criterion)
  - every rule's id starts with `enduro.` (prevents collisions
    with the existing `br.*` braking rules)
  - every rule is in the `strategy` category — endurance scoring
    is multi-lap planning, not single-corner technique
  - subcategories cover the spec areas (stint management, tire,
    fuel, pace decay) so a future "added 5 throttle rules and
    called them endurance" mistake gets caught

Mutation: deleting any single rule file should drop the count
below 5 and fail the count assertion. The strategy-category and
prefix assertions catch a misplaced rule from another dimension.
"""

from __future__ import annotations

from pathlib import Path

from coaching.library.rule_loader import load_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
ENDURANCE_DIR = (
    REPO_ROOT / "coaching" / "library" / "rules" / "endurance"
)


def test_endurance_rules_directory_loads_cleanly() -> None:
    """Every endurance YAML must validate against RuleSpec.
    Pin: a malformed YAML or a schema violation would crash the
    engine at startup; the loader must never raise on this path."""
    rules = load_rules(ENDURANCE_DIR)
    assert rules, "expected at least one endurance rule"


def test_at_least_five_endurance_rules_authored() -> None:
    """T074 acceptance: 5+ rules. Pin the count — losing one
    in a refactor would silently weaken endurance coaching."""
    rules = load_rules(ENDURANCE_DIR)
    assert len(rules) >= 5, (
        f"expected at least 5 endurance rules, found {len(rules)}: "
        f"{[r.id for r in rules]}"
    )


def test_every_endurance_rule_id_starts_with_enduro_prefix() -> None:
    """Prefix collision check. A future contributor copying a
    `br.*` braking template into the endurance dir without
    renaming the id would silently shadow a braking rule via
    the loader's id-uniqueness check, and the rules engine would
    misroute evidence. Pin the namespacing convention."""
    rules = load_rules(ENDURANCE_DIR)
    bad = [r.id for r in rules if not r.id.startswith("enduro.")]
    assert not bad, f"non-endurance ids in endurance dir: {bad}"


def test_every_endurance_rule_is_in_strategy_category() -> None:
    """Endurance scoring is multi-lap planning (stint management,
    fuel target, tire conservation), not single-corner technique.
    Calling these `technique` would conflate them with braking/
    cornering rules and mis-rank them in the prescription queue."""
    rules = load_rules(ENDURANCE_DIR)
    bad = [(r.id, r.category) for r in rules if r.category != "strategy"]
    assert not bad, f"endurance rules with non-strategy category: {bad}"


def test_endurance_subcategories_cover_spec_areas() -> None:
    """Spec calls out stint management AND tire. Sweep
    subcategories and pin that BOTH families are represented —
    a directory of five "tire abuse" variants would technically
    pass count+prefix+category, but would fail the spec intent."""
    rules = load_rules(ENDURANCE_DIR)
    subcats = {r.subcategory for r in rules}
    has_stint = any(
        "stint" in s for s in subcats
    ) or any("pace" in s for s in subcats)
    has_tire = any("tire" in s for s in subcats)
    assert has_stint, (
        f"endurance rules missing stint/pace coverage; "
        f"subcategories: {sorted(subcats)}"
    )
    assert has_tire, (
        f"endurance rules missing tire coverage; "
        f"subcategories: {sorted(subcats)}"
    )
