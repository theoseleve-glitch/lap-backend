"""T204 — bundle signing helpers.

`coaching.library.bundle` builds and signs the JSON payload the
T204 endpoint serves to clients. Three concerns:

1. `canonical_json` — deterministic byte representation so the
   server's hash matches what the client recomputes.
2. `bundle_version` — content-addressed version id (SHA-256 hex
   of the canonical unsigned body). Same rules → same version.
3. `sign_bundle` / `verify_bundle` — HMAC-SHA256 over the
   canonical signed body (rules + version). Mirrors the existing
   Svix/Stripe webhook signing posture in this repo.

The endpoint glue (config plumbing, query-param handling, 503
on missing secret) lives in test_coaching_rules_bundle_endpoint.
This file pins the crypto/data primitives.
"""

import hashlib
import hmac
import json

import pytest

from coaching.library.bundle import (
    BundleVerifyError,
    bundle_version,
    canonical_json,
    sign_bundle,
    verify_bundle,
)


def test_canonical_json_sorts_keys() -> None:
    """Same dict in different insertion orders must produce the
    same bytes. Otherwise client and server hashes diverge."""
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b
    assert a == b'{"a":2,"b":1}'


def test_canonical_json_uses_compact_separators() -> None:
    """No whitespace — every byte counts toward hash equality."""
    out = canonical_json({"a": 1, "b": [2, 3]})
    assert b" " not in out
    assert b"\n" not in out


def test_canonical_json_recurses_into_nested_dicts() -> None:
    out = canonical_json({"outer": {"z": 1, "a": 2}})
    assert out == b'{"outer":{"a":2,"z":1}}'


def test_bundle_version_is_sha256_hex_of_canonical_body() -> None:
    """Spec the contract — clients can recompute the same hash if
    they canonical-JSON the unsigned body and SHA-256 it."""
    unsigned = {"format_version": 1, "rules": []}
    expected = hashlib.sha256(canonical_json(unsigned)).hexdigest()
    assert bundle_version(unsigned) == expected


def test_bundle_version_is_deterministic() -> None:
    """Same input → same hash, key insertion order irrelevant."""
    a = bundle_version({"format_version": 1, "rules": [{"id": "x"}]})
    b = bundle_version({"rules": [{"id": "x"}], "format_version": 1})
    assert a == b


def test_bundle_version_changes_with_rules_content() -> None:
    """Different rules → different version. Content-addressed
    means clients can compare by string equality and skip
    re-downloading when nothing changed."""
    a = bundle_version({"format_version": 1, "rules": []})
    b = bundle_version(
        {"format_version": 1, "rules": [{"id": "x"}]}
    )
    assert a != b


def test_sign_bundle_adds_bundle_version_and_signature() -> None:
    """sign_bundle is idempotent on input — returns a NEW dict
    with bundle_version and signature appended; original is
    untouched (caller may want to log the unsigned form)."""
    unsigned = {"format_version": 1, "rules": []}
    signed = sign_bundle(unsigned, secret=b"shh")
    assert "bundle_version" in signed
    assert "signature" in signed
    assert "bundle_version" not in unsigned
    assert "signature" not in unsigned


def test_sign_bundle_signature_block_carries_alg_and_value() -> None:
    signed = sign_bundle(
        {"format_version": 1, "rules": []}, secret=b"shh"
    )
    sig = signed["signature"]
    assert sig["alg"] == "HMAC-SHA256"
    assert isinstance(sig["value"], str)
    # SHA-256 hex = 64 chars
    assert len(sig["value"]) == 64


def test_sign_bundle_signature_is_hmac_over_signed_body() -> None:
    """The signature covers everything except itself — including
    bundle_version. A client tampering with bundle_version (e.g.
    serving rules under a stale version id) breaks the HMAC."""
    secret = b"shh"
    signed = sign_bundle(
        {"format_version": 1, "rules": []}, secret=secret
    )
    sig = signed.pop("signature")
    expected = hmac.new(
        secret, canonical_json(signed), hashlib.sha256
    ).hexdigest()
    assert sig["value"] == expected


def test_verify_bundle_accepts_unmodified_bundle() -> None:
    secret = b"shh"
    signed = sign_bundle(
        {"format_version": 1, "rules": [{"id": "x"}]}, secret=secret
    )
    verify_bundle(signed, secret=secret)  # no raise = pass


def test_verify_bundle_returns_true_for_valid() -> None:
    """Verify also returns True for valid bundles — convenient
    for test assertions and admin tools that want a bool."""
    secret = b"shh"
    signed = sign_bundle(
        {"format_version": 1, "rules": []}, secret=secret
    )
    assert verify_bundle(signed, secret=secret) is True


def test_verify_bundle_raises_when_body_tampered() -> None:
    secret = b"shh"
    signed = sign_bundle(
        {"format_version": 1, "rules": []}, secret=secret
    )
    signed["rules"].append({"id": "injected"})
    with pytest.raises(BundleVerifyError):
        verify_bundle(signed, secret=secret)


def test_verify_bundle_raises_when_bundle_version_tampered() -> None:
    """Spoofing the bundle_version (e.g. claiming a stale version
    id while serving newer rules) must fail verification."""
    secret = b"shh"
    signed = sign_bundle(
        {"format_version": 1, "rules": []}, secret=secret
    )
    signed["bundle_version"] = "0" * 64
    with pytest.raises(BundleVerifyError):
        verify_bundle(signed, secret=secret)


def test_verify_bundle_raises_with_wrong_secret() -> None:
    signed = sign_bundle(
        {"format_version": 1, "rules": []}, secret=b"shh"
    )
    with pytest.raises(BundleVerifyError):
        verify_bundle(signed, secret=b"wrong")


def test_verify_bundle_raises_when_signature_missing() -> None:
    """Defense against stripped-signature attacks — a payload
    without `signature` must fail verification, not silently pass."""
    unsigned = {"format_version": 1, "rules": []}
    with pytest.raises(BundleVerifyError):
        verify_bundle(unsigned, secret=b"shh")


def test_signed_bundle_is_json_serializable() -> None:
    """End-to-end wire-format guarantee: anything sign_bundle
    produces must round-trip through json.dumps/loads losslessly."""
    signed = sign_bundle(
        {"format_version": 1, "rules": [{"id": "x", "version": 1}]},
        secret=b"shh",
    )
    s = json.dumps(signed)
    assert json.loads(s) == signed
