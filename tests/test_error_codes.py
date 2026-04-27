"""T187 (sprint2-backend) — ErrorCode enum coverage tests.

Two pins:

  1. **Wire shape unchanged.** `ErrorCode.X` JSON-serializes to
     the literal string "x" the API has been returning since
     before T187. Migration must NOT change response bodies.

  2. **Catalog completeness.** Every `detail="..."` literal still
     present in `app/routers/*.py` and `app/deps.py` must have
     a corresponding ErrorCode member. As routers migrate to use
     ErrorCode.X, this regex finds zero literal `detail="..."`
     calls and the assertion becomes vacuously true — that's the
     end state. Until then, this test surfaces any *new* literal
     code that snuck in without enum membership.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import HTTPException

from app.error_codes import ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_error_code_str_value_matches_legacy_detail_string() -> None:
    """StrEnum members are str subclasses — `ErrorCode.NOT_FOUND`
    is the string "session_not_found" anywhere a str is expected.
    Pin this so a future "let's just use IntEnum" edit fails."""
    assert ErrorCode.SESSION_NOT_FOUND == "session_not_found"
    assert isinstance(ErrorCode.SESSION_NOT_FOUND, str)


def test_error_code_serializes_to_json_string() -> None:
    """The end-to-end concern: HTTPException(detail=ErrorCode.X)
    must produce the same JSON body shape as
    HTTPException(detail='x'). FastAPI serializes the detail via
    `json.dumps`; pin that step here to catch any future regression
    where a custom encoder breaks the wire format."""
    assert (
        json.dumps({"detail": ErrorCode.NOT_AUTHORIZED})
        == '{"detail": "not_authorized"}'
    )


def test_http_exception_accepts_error_code_directly() -> None:
    """FastAPI's HTTPException stores `detail` verbatim. A
    StrEnum member is a str, so it stores cleanly — and equates
    to the legacy literal at the assertion level."""
    exc = HTTPException(status_code=404, detail=ErrorCode.SESSION_NOT_FOUND)
    assert exc.detail == "session_not_found"


def test_no_literal_detail_strings_in_routers() -> None:
    """Migration completeness: zero literal `detail="..."` strings
    should remain in `app/routers/*.py`, `app/deps.py`, and
    `app/rate_limit.py`. Every error code goes through ErrorCode.

    A future PR adding a new error must define the code in
    `app/error_codes.py` and reference it as `detail=ErrorCode.X`
    rather than re-introducing a literal — this test catches the
    regression.
    """
    pattern = re.compile(r'detail="([a-z][a-z0-9_]+)"')
    paths = [
        *(REPO_ROOT / "app" / "routers").glob("*.py"),
        REPO_ROOT / "app" / "deps.py",
        REPO_ROOT / "app" / "rate_limit.py",
    ]
    holdouts: dict[str, set[str]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            holdouts.setdefault(path.name, set()).add(match.group(1))
    assert not holdouts, (
        "literal detail strings remain (migrate to ErrorCode): "
        + ", ".join(
            f"{name}: {sorted(codes)}"
            for name, codes in holdouts.items()
        )
    )


def test_no_two_enum_members_share_a_value() -> None:
    """Each wire-shape string maps to exactly one Python name.
    StrEnum allows aliasing (two members with the same value)
    but for an error catalog that's a hazard — debug logs would
    surface different-looking names for the same code."""
    values = [member.value for member in ErrorCode]
    assert len(values) == len(set(values)), (
        f"duplicate values in ErrorCode: {values}"
    )
