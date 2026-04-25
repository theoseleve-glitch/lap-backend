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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from lap_schemas.v1 import CoachingRuleList, CoachingRulePublic

from app.config import Config
from app.deps import get_config
from coaching.library.bundle import sign_bundle
from coaching.library.compiler import compile_library
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


# T204 — full compiled+signed bundle for client engines.
# `version` only accepts "latest" today; the param exists for
# T-future to support pinned-version retrieval without breaking
# the wire shape now.
_SUPPORTED_BUNDLE_VERSIONS = frozenset({"latest"})


@router.get("/rules/bundle")
def get_rules_bundle(
    cfg: Annotated[Config, Depends(get_config)],
    version: str = Query(default="latest"),
) -> dict[str, Any]:
    if version not in _SUPPORTED_BUNDLE_VERSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported_bundle_version: {version!r}",
        )
    secret = cfg.coaching_bundle_signing_secret.get_secret_value()
    if not secret:
        # Refusing to ship unsigned is the correct posture — easier
        # to debug a 503 than discover later that production has
        # been serving unsigned payloads. Mirrors stripe webhook 503.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="coaching_bundle_signing_secret_not_configured",
        )
    rules = load_rules(_RULES_DIR)
    unsigned = compile_library(rules)
    return sign_bundle(unsigned, secret=secret.encode("utf-8"))
