"""T182 — coaching rule loader.

Walks a rules directory recursively, parses each `*.yaml`, and
validates against `RuleSpec` (T181). Three failure modes get
unified under `RuleLoadError`:
  - YAML syntax error          → `yaml.YAMLError`
  - Schema validation failure  → `pydantic.ValidationError`
  - Duplicate rule id          → loader-detected
All carry the offending file path so operators can fix the YAML.
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from coaching.library.rule_schema import RuleSpec


class RuleLoadError(Exception):
    """Raised when a rule file fails to parse, validate, or
    introduces a duplicate id."""


def load_rules(rules_dir: Path) -> list[RuleSpec]:
    """Recursively load and validate every `*.yaml` under
    `rules_dir`. Returns the list of `RuleSpec` instances. Raises
    `RuleLoadError` on any failure with the file path in the
    message.
    """
    if not rules_dir.exists():
        raise RuleLoadError(f"rules directory not found: {rules_dir}")
    if not rules_dir.is_dir():
        raise RuleLoadError(f"rules path is not a directory: {rules_dir}")

    seen_ids: dict[str, Path] = {}
    rules: list[RuleSpec] = []

    for path in sorted(rules_dir.rglob("*.yaml")):
        rules.append(_load_one(path, seen_ids))

    return rules


def _load_one(path: Path, seen_ids: dict[str, Path]) -> RuleSpec:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuleLoadError(
            f"{path}: invalid YAML syntax — {exc}"
        ) from exc

    try:
        rule = RuleSpec.model_validate(raw)
    except ValidationError as exc:
        raise RuleLoadError(
            f"{path}: failed schema validation — {exc}"
        ) from exc

    if rule.id in seen_ids:
        first = seen_ids[rule.id]
        raise RuleLoadError(
            f"duplicate rule id {rule.id!r}: {first} and {path}"
        )
    seen_ids[rule.id] = path
    return rule
