"""T187 (sprint2-backend) — canonical ErrorCode enum.

Single source of truth for the `detail` strings the API returns
in HTTPException bodies. Every member's value is the wire-shape
string clients see — preserving the existing API contract while
giving the server side type safety + a checkable list of codes.

Why an enum? Two reasons:
  1. Single source of truth — adding a new error code goes
     through this file, where adjacent codes provide naming
     guidance ("session_not_found", not "could_not_find_session").
  2. Static checkability — a typo in `detail=ErrorCode.NOT_FUOND`
     fails at import time, not at runtime when an end-user is
     looking at a 500 instead of the intended 404.

`StrEnum` makes each member a `str` subclass, so usage in
`HTTPException(detail=ErrorCode.X)` JSON-serializes identically
to the legacy `detail="x"` strings — zero wire-shape change at
migration time.

Categories (organizational, not enforced — codes are flat by
design so a single namespace prevents accidental name shadowing):
  - Auth / authorization
  - Subjects (driver, user, session, prescription, insight, ...)
  - Org / invitations
  - Catalog / template
  - Billing / Stripe
  - Coaching
  - Pairing
  - Validation
  - Rate limiting
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    # -- auth / authorization -------------------------------------
    INVALID_TOKEN = "invalid_token"
    ADMIN_REQUIRED = "admin_required"
    ADMIN_ONLY = "admin_only"
    SUBSCRIPTION_REQUIRED = "subscription_required"
    NOT_AUTHORIZED = "not_authorized"
    NOT_ORG_ADMIN = "not_org_admin"
    NOT_ORG_MEMBER = "not_org_member"
    EMAIL_MISMATCH = "email_mismatch"

    # -- subjects: not-found shapes ------------------------------
    DRIVER_NOT_FOUND = "driver_not_found"
    USER_NOT_FOUND = "user_not_found"
    SESSION_NOT_FOUND = "session_not_found"
    PRESCRIPTION_NOT_FOUND = "prescription_not_found"
    INSIGHT_NOT_FOUND = "insight_not_found"
    WEAKNESS_NOT_FOUND = "weakness_not_found"
    TRANSFER_LINK_NOT_FOUND = "transfer_link_not_found"
    ENDURANCE_EVENT_NOT_FOUND = "endurance_event_not_found"
    TARGET_EVENT_NOT_FOUND = "target_event_not_found"
    TEMPLATE_NOT_FOUND = "template_not_found"
    INVITATION_NOT_FOUND = "invitation_not_found"
    MEMBER_NOT_FOUND = "member_not_found"
    TRACK_NOT_FOUND = "track_not_found"
    CAR_NOT_FOUND = "car_not_found"

    # -- session ingest / bridging --------------------------------
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    MANIFEST_INVALID_JSON = "manifest_invalid_json"
    MANIFEST_INVALID = "manifest_invalid"
    CHUNK_FILENAME_INVALID = "chunk_filename_invalid"
    CHUNK_CHECKSUM_MISMATCH = "chunk_checksum_mismatch"
    SESSION_UNBRIDGEABLE = "session_unbridgeable"
    INCOMPATIBLE_TRACK = "incompatible_track"
    INCOMPATIBLE_CAR_CLASS = "incompatible_car_class"
    DUPLICATE_LINK = "duplicate_link"

    # -- session strategy / templates ----------------------------
    STRATEGY_PLAN_ALREADY_EXISTS = "strategy_plan_already_exists"
    DUPLICATE_TEMPLATE_NAME = "duplicate_template_name"

    # -- org invitations ------------------------------------------
    ALREADY_MEMBER = "already_member"
    ALREADY_ACCEPTED = "already_accepted"
    INVITATION_EXPIRED = "invitation_expired"
    CANNOT_REMOVE_ADMIN = "cannot_remove_admin"
    # T195 — DELETE /api/v1/me blocks if the caller still admins
    # an org. Schema FK on organizations.admin_user_id is non-
    # cascade by design (orphaning members + billing would be
    # worse than blocking) — this code surfaces the block to the
    # client so they can transfer admin first.
    CANNOT_DELETE_ORG_ADMIN = "cannot_delete_org_admin"

    # -- billing / Stripe -----------------------------------------
    STRIPE_PRICE_NOT_CONFIGURED = "stripe_price_not_configured"
    NO_STRIPE_CUSTOMER = "no_stripe_customer"
    MISSING_STRIPE_SIGNATURE = "missing_stripe_signature"
    STRIPE_WEBHOOK_SECRET_NOT_CONFIGURED = (
        "stripe_webhook_secret_not_configured"
    )
    INVALID_STRIPE_SIGNATURE = "invalid_stripe_signature"

    # -- coaching --------------------------------------------------
    COACHING_BUNDLE_SIGNING_SECRET_NOT_CONFIGURED = (
        "coaching_bundle_signing_secret_not_configured"
    )

    # -- pairing ---------------------------------------------------
    INVALID_PAIRING_TOKEN = "invalid_pairing_token"

    # -- validation / rate-limit ----------------------------------
    WINDOW_MUST_BE_POSITIVE = "window_must_be_positive"
    RATE_LIMITED = "rate_limited"
