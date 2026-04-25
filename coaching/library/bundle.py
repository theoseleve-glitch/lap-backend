"""T204 — bundle signing helpers.

Wire-format primitives for the rules bundle endpoint. Three jobs:

  1. `canonical_json(payload)` — deterministic byte form so the
     server's hash matches what the client recomputes. Sorted
     keys + compact separators; recurses into nested dicts via
     json.dumps's own sort_keys.
  2. `bundle_version(unsigned)` — content-addressed version id
     (SHA-256 hex of the canonical unsigned body). Same rules →
     same version, no server-side counter needed.
  3. `sign_bundle` / `verify_bundle` — HMAC-SHA256 over the
     canonical signed body (rules + bundle_version + format_version).
     Mirrors the existing webhook signing posture used for Stripe
     and Clerk/Svix; clients verify with the shared secret.

The signature covers EVERYTHING except the signature block
itself, including `bundle_version` — so a peer that swaps in
stale rules under a faked-current version id breaks the HMAC.
"""

import hashlib
import hmac
import json
from typing import Any

_SIGNATURE_ALG = "HMAC-SHA256"


class BundleVerifyError(Exception):
    """Raised when a signed bundle fails HMAC verification — bad
    secret, tampered body, or missing signature block."""


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Encode `payload` as canonical JSON bytes: sorted keys,
    no whitespace, UTF-8. Used for both content-hashing and
    signature input so server and client agree byte-for-byte."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def bundle_version(unsigned: dict[str, Any]) -> str:
    """SHA-256 hex of the canonical unsigned body. Acts as the
    content-addressed version id — clients compare by string
    equality and skip re-downloading when nothing changed."""
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def sign_bundle(
    unsigned: dict[str, Any], *, secret: bytes
) -> dict[str, Any]:
    """Build a signed bundle from `unsigned`. Returns a NEW dict
    with `bundle_version` and `signature` appended; the input
    dict is not mutated, so callers can keep the unsigned form
    for diagnostics."""
    signed: dict[str, Any] = dict(unsigned)
    signed["bundle_version"] = bundle_version(unsigned)
    mac = hmac.new(
        secret, canonical_json(signed), hashlib.sha256
    ).hexdigest()
    signed["signature"] = {"alg": _SIGNATURE_ALG, "value": mac}
    return signed


def verify_bundle(signed: dict[str, Any], *, secret: bytes) -> bool:
    """Verify the HMAC on a signed bundle. Returns True; raises
    `BundleVerifyError` on missing/wrong signature, tampered body,
    or wrong secret. Constant-time compare via `hmac.compare_digest`."""
    sig_block = signed.get("signature")
    if not isinstance(sig_block, dict) or "value" not in sig_block:
        raise BundleVerifyError("missing or malformed signature block")
    claimed = sig_block["value"]
    body_for_hash = {k: v for k, v in signed.items() if k != "signature"}
    expected = hmac.new(
        secret, canonical_json(body_for_hash), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(claimed, expected):
        raise BundleVerifyError("signature does not match body")
    return True
