"""T185 — coaching rule runner.

Integrates schema (T181), compiler (T183), and namespace (T184)
to evaluate rules against a session's feature context. For each
rule whose condition fires, emits one `CoachingInsight` with the
explanation rendered for the requested difficulty level.

Mustache-style `{{var}}` templating instead of Jinja2 — ADR-013
templates don't need conditionals or loops, so a regex
substitution beats a 200KB dep.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from coaching.library.condition_compiler import compile_condition
from coaching.library.rule_schema import Difficulty, RuleSpec

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class InsightRenderError(Exception):
    """Raised when a rule's template references a variable the
    context doesn't provide. Loud failure beats silent blanks."""


@dataclass(frozen=True)
class CoachingInsight:
    rule_id: str
    rule_version: int
    category: str
    subcategory: str
    difficulty: Difficulty
    explanation: str


def run_rules(
    rules: Iterable[RuleSpec],
    *,
    context: dict[str, Any],
    difficulty: Difficulty = "beginner",
) -> list[CoachingInsight]:
    insights: list[CoachingInsight] = []
    for rule in rules:
        compiled = compile_condition(rule.detection.condition)
        if not compiled.predicate(context):
            continue
        template = _select_template(rule, difficulty)
        explanation = _render(template, context, rule_id=rule.id)
        insights.append(
            CoachingInsight(
                rule_id=rule.id,
                rule_version=rule.version,
                category=rule.category,
                subcategory=rule.subcategory,
                difficulty=difficulty,
                explanation=explanation,
            )
        )
    return insights


def _select_template(rule: RuleSpec, difficulty: Difficulty) -> str:
    template: str = getattr(rule.explanation_template, difficulty)
    return template


def _render(
    template: str, context: dict[str, Any], *, rule_id: str
) -> str:
    """Substitute every `{{var}}` from `context`. Missing keys
    raise so the operator notices a broken rule before drivers
    see blank coaching messages."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            raise InsightRenderError(
                f"rule {rule_id!r}: template references "
                f"{name!r} but context has no value for it"
            )
        return str(context[name])

    return _PLACEHOLDER.sub(replace, template)
