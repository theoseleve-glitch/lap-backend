"""T181 — coaching rule schema (ADR-013).

Pydantic v2 models that mirror the locked YAML rule format. The
loader (T182) feeds parsed YAML into `RuleSpec.model_validate`;
malformed rules raise at startup rather than silently misfiring at
runtime.

`RULE_JSON_SCHEMA` is the JSON Schema artifact `model_json_schema()`
emits — downstream tools (the docs site, editor tooling, the
loader's error messages) consume it without re-deriving.

Why Pydantic over a hand-rolled JSON Schema file: one source of
truth. If a field is added to the model, the JSON Schema updates
automatically. A hand-maintained `.json` would drift the moment the
model changed.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Difficulty = Literal["beginner", "intermediate", "advanced"]
CornerPriority = Literal["low", "medium", "high"]
Category = Literal["technique", "setup", "strategy"]
Comparison = Literal["gte", "gt", "lte", "lt", "eq"]


class _StrictModel(BaseModel):
    """Base model: extra="forbid" so typos in YAML keys surface at
    load time instead of being silently ignored."""

    model_config = ConfigDict(extra="forbid")


class DetectionContext(_StrictModel):
    must_have_channels: list[str] = Field(min_length=1)
    min_corner_priority: CornerPriority


class EvidenceRequired(_StrictModel):
    min_occurrences_in_session: int = Field(ge=1)
    min_sessions_in_window: int = Field(ge=1)
    window_days: int = Field(ge=1)


class Detection(_StrictModel):
    signal: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    context: DetectionContext
    evidence_required: EvidenceRequired


class ExplanationTemplate(_StrictModel):
    beginner: str = Field(min_length=1)
    intermediate: str = Field(min_length=1)
    advanced: str = Field(min_length=1)


class SuccessCondition(_StrictModel):
    metric: str = Field(min_length=1)
    comparison: Comparison
    target_pct: float = Field(gt=0, le=2.0)
    measurement_window: str = Field(min_length=1)


class Prescription(_StrictModel):
    drill_template_id: str = Field(min_length=1)
    expected_gain_kmh: str = Field(min_length=1)
    success_condition: SuccessCondition


class SimVsReal(_StrictModel):
    likely_transfers: bool
    caveats: list[str] = Field(default_factory=list)


class TransferProfile(_StrictModel):
    sim_vs_real: SimVsReal


class RuleSpec(_StrictModel):
    """A single coaching rule as loaded from YAML."""

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    category: Category
    subcategory: str = Field(min_length=1)
    difficulty: Difficulty
    detection: Detection
    severity_rule: str = Field(min_length=1)
    explanation_template: ExplanationTemplate
    prescription: Prescription
    # `transfer_profile` is the only optional top-level field —
    # ADR-013 shows it but a rule that hasn't been characterized
    # for sim-vs-real yet is still a usable rule.
    transfer_profile: TransferProfile | None = None


RULE_JSON_SCHEMA: dict[str, Any] = RuleSpec.model_json_schema()
