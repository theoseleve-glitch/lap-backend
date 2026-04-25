"""Public coaching endpoints (T202+).

Read-only surface over the coaching rules library. Auth-free —
ADR-009 ships the same rules to clients via the bundle endpoint
(T204) anyway, so the index is non-sensitive reference data the
web app can fetch before login.

Companion to the admin simulate endpoint (T201): admin tests rule
behavior against a hand-crafted feature dict; public callers list
the rule reference shape (id/version/category/subcategory/difficulty).
Full rule content (templates, conditions, prescriptions) only ships
via the bundle endpoint, not here.
"""

from pathlib import Path

from fastapi import APIRouter
from lap_schemas.v1 import CoachingRuleList, CoachingRulePublic

from coaching.library.rule_loader import load_rules

router = APIRouter(prefix="/api/v1/coaching", tags=["coaching"])

_RULES_DIR = (
    Path(__file__).resolve().parents[2]
    / "coaching"
    / "library"
    / "rules"
)


@router.get("/rules", response_model=CoachingRuleList)
def list_rules() -> CoachingRuleList:
    rules = load_rules(_RULES_DIR)
    items = tuple(
        CoachingRulePublic(
            id=rule.id,
            version=rule.version,
            category=rule.category,
            subcategory=rule.subcategory,
            difficulty=rule.difficulty,
        )
        for rule in rules
    )
    return CoachingRuleList(items=items, total=len(items))
