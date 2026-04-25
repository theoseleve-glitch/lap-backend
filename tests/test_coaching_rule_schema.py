"""T181 — JSON Schema for the coaching rule YAML format.

The schema is the contract between the human-authored library
(ADR-013) and the rules loader (T182). The example from ADR-013
is the canonical fixture: if it stops validating, either the ADR
or the schema drifted, and that's a bug we want caught at CI.

Implementation uses Pydantic v2 — `RuleSpec.model_json_schema()`
generates the JSON Schema artifact downstream tools consume.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from coaching.library.rule_schema import (
    RULE_JSON_SCHEMA,
    RuleSpec,
)

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "coaching"
    / "adr_013_example.yaml"
)


def _load_example() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_adr_013_example_validates() -> None:
    """The locked ADR example MUST parse cleanly. If this breaks,
    either the ADR or the schema drifted — fix the schema."""
    rule = RuleSpec.model_validate(_load_example())
    assert rule.id == "br.late_braking.apex_speed_loss"
    assert rule.version == 1
    assert rule.detection.signal == "apex_speed"


def test_missing_id_is_rejected() -> None:
    bad = _load_example()
    del bad["id"]
    with pytest.raises(ValidationError):
        RuleSpec.model_validate(bad)


def test_missing_detection_is_rejected() -> None:
    bad = _load_example()
    del bad["detection"]
    with pytest.raises(ValidationError):
        RuleSpec.model_validate(bad)


def test_invalid_difficulty_is_rejected() -> None:
    """ADR-013 commits to beginner/intermediate/advanced. Anything
    else can't be rendered through the explanation_template
    branches, so reject early."""
    bad = _load_example()
    bad["difficulty"] = "expert"
    with pytest.raises(ValidationError):
        RuleSpec.model_validate(bad)


def test_invalid_min_corner_priority_is_rejected() -> None:
    bad = _load_example()
    bad["detection"]["context"]["min_corner_priority"] = "ultra"
    with pytest.raises(ValidationError):
        RuleSpec.model_validate(bad)


def test_negative_window_days_is_rejected() -> None:
    """A non-positive window is meaningless — ge=1 enforces it."""
    bad = _load_example()
    bad["detection"]["evidence_required"]["window_days"] = 0
    with pytest.raises(ValidationError):
        RuleSpec.model_validate(bad)


def test_explanation_template_requires_all_three_difficulties() -> None:
    """Every rule must speak to every learner level. The renderer
    selects by `difficulty` at runtime; missing branches would NPE."""
    bad = _load_example()
    del bad["explanation_template"]["intermediate"]
    with pytest.raises(ValidationError):
        RuleSpec.model_validate(bad)


def test_unknown_top_level_key_is_rejected() -> None:
    """`extra="forbid"` catches typos like `descrption` instead of
    `description` — silent ignore would let a rule render no text."""
    bad = _load_example()
    bad["typo_field"] = "oops"
    with pytest.raises(ValidationError):
        RuleSpec.model_validate(bad)


def test_rule_json_schema_is_a_published_artifact() -> None:
    """`RULE_JSON_SCHEMA` is the artifact downstream tools (loader,
    docs site) consume. It must look like JSON Schema — top-level
    object with `properties` and `required`."""
    assert isinstance(RULE_JSON_SCHEMA, dict)
    assert RULE_JSON_SCHEMA.get("type") == "object"
    assert "properties" in RULE_JSON_SCHEMA
    required = RULE_JSON_SCHEMA.get("required", [])
    for field in (
        "id",
        "version",
        "category",
        "subcategory",
        "difficulty",
        "detection",
        "severity_rule",
        "explanation_template",
        "prescription",
    ):
        assert field in required, f"missing required field: {field}"
