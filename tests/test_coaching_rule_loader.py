"""T182 — coaching rule loader.

Walks `coaching/library/rules/**/*.yaml`, validates each against
`RuleSpec` from T181, returns the list. Malformed rules raise at
load time so operators see startup errors, not silent runtime
no-ops.

Duplicate rule IDs across files raise: silently shadowing one
rule with another (whichever loaded last) is the kind of bug
that takes days to find.
"""

from pathlib import Path

import pytest

from coaching.library.rule_loader import (
    RuleLoadError,
    load_rules,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "coaching"
    / "adr_013_example.yaml"
)


def _write_rule(
    target_dir: Path, name: str, *, rule_id: str = "br.test.foo"
) -> None:
    """Write a copy of the ADR-013 fixture under `target_dir/name`,
    overriding the `id` field so the loader sees distinct rules."""
    target_dir.mkdir(parents=True, exist_ok=True)
    src = ADR_FIXTURE.read_text(encoding="utf-8")
    # Cheap id substitution — the fixture's id is on the first line.
    out = src.replace(
        'id: "br.late_braking.apex_speed_loss"',
        f'id: "{rule_id}"',
    )
    (target_dir / name).write_text(out, encoding="utf-8")


def test_empty_directory_returns_empty_list(tmp_path: Path) -> None:
    rules = load_rules(tmp_path)
    assert rules == []


def test_loads_single_valid_rule(tmp_path: Path) -> None:
    _write_rule(tmp_path, "rule_one.yaml", rule_id="br.test.one")
    rules = load_rules(tmp_path)
    assert len(rules) == 1
    assert rules[0].id == "br.test.one"


def test_loads_rules_across_subdirectories(tmp_path: Path) -> None:
    """Real library will be organized by category — `braking/`,
    `apex/`, `traction/`. Loader must recurse."""
    _write_rule(tmp_path / "braking", "a.yaml", rule_id="br.x.a")
    _write_rule(tmp_path / "apex", "b.yaml", rule_id="ap.y.b")
    rules = load_rules(tmp_path)
    ids = {r.id for r in rules}
    assert ids == {"br.x.a", "ap.y.b"}


def test_malformed_rule_raises_with_path_in_message(
    tmp_path: Path,
) -> None:
    """Operator needs the file path to find and fix the typo."""
    bad = tmp_path / "broken.yaml"
    bad.write_text(
        "id: missing-everything-else\n", encoding="utf-8"
    )
    with pytest.raises(RuleLoadError) as exc_info:
        load_rules(tmp_path)
    assert "broken.yaml" in str(exc_info.value)


def test_duplicate_rule_id_raises(tmp_path: Path) -> None:
    """Two YAMLs with the same id → silent shadow at runtime.
    Catch at load."""
    _write_rule(tmp_path, "first.yaml", rule_id="br.dupe.x")
    _write_rule(tmp_path, "second.yaml", rule_id="br.dupe.x")
    with pytest.raises(RuleLoadError) as exc_info:
        load_rules(tmp_path)
    assert "br.dupe.x" in str(exc_info.value)


def test_non_yaml_files_are_ignored(tmp_path: Path) -> None:
    """README.md or stray .json files in the rules dir shouldn't
    crash the loader."""
    _write_rule(tmp_path, "real.yaml", rule_id="br.real.one")
    (tmp_path / "README.md").write_text("# notes", encoding="utf-8")
    (tmp_path / "schema.json").write_text("{}", encoding="utf-8")
    rules = load_rules(tmp_path)
    assert len(rules) == 1


def test_invalid_yaml_syntax_raises_with_path(tmp_path: Path) -> None:
    """Distinguish 'YAML didn't parse' from 'schema rejected it'
    — both should mention the file. yaml.YAMLError vs
    ValidationError get unified under RuleLoadError."""
    bad = tmp_path / "bad_syntax.yaml"
    bad.write_text(
        "id: [unclosed\n", encoding="utf-8"
    )
    with pytest.raises(RuleLoadError) as exc_info:
        load_rules(tmp_path)
    assert "bad_syntax.yaml" in str(exc_info.value)


def test_missing_directory_raises(tmp_path: Path) -> None:
    """A typo'd path or missing checkout shouldn't silently load
    zero rules — that's how 'why isn't my coach saying anything'
    happens."""
    missing = tmp_path / "does_not_exist"
    with pytest.raises(RuleLoadError):
        load_rules(missing)
