# Security Audit — lap-backend (T181)

**Date:** 2026-04-27
**Scope:** all FastAPI routers under `app/routers/` (24 modules)
**Categories:** missing auth, IDOR (insecure direct object reference),
privilege escalation, status-parity (existence-leak via 404 vs 403)
**Methodology:** static review of every endpoint decorator and handler
body. Each handler classified by (a) does it use `Depends(get_current_user)`
or another auth dep, (b) for resources loaded by UUID path param does
the handler verify caller ownership, (c) for org/role-scoped endpoints
does the handler check membership and role.

## Findings

| Router | Endpoint | Severity | Category | Finding |
|---|---|---|---|---|
| `endurance.py` | `GET /api/v1/endurance/events/{event_id}` | **HIGH** | IDOR | `get_endurance_event_summary` authenticates via `_user: ... Depends(get_current_user)` but the leading-underscore name signals the user is intentionally unused — and indeed never checked against `event.driver_ids`. Any authenticated user can read any endurance event's full driver roster, stint count, total duration, and aggregates over linked sessions (mean lap-time decay, mean fuel used). Recommend: load the calling user's `DriverProfile.id`, then `if str(profile.id) not in event.driver_ids: raise 404`. |
| `jobs.py` | `GET /api/v1/jobs/{job_id}/events` (SSE) | **HIGH** | IDOR | `job_events` takes a `job_id: str` path param and authenticates the caller (line 108) but explicitly discards the user via `_ = user` (line 115). The job_id is never linked back to the caller. Any authenticated user can subscribe to any other user's ingest job event stream and observe processing-stage progress, error reasons, and completion timing. Recommend: extend `JobEventBusProtocol` (or a side-table) so a job_id is resolvable to its owning user_id, then 404 on mismatch with status parity. |
| `coaching.py` | `GET /api/v1/coaching/rules` | LOW | Missing auth | Public per docstring (ADR-009 — clients fetch the rule library before login). Defensible as non-sensitive reference data, but not in the explicit "expected public" allowlist (catalog, pairing/redeem, webhooks). Confirm with product whether the rule list is differentiating IP. |
| `coaching.py` | `GET /api/v1/coaching/rules/bundle` | LOW | Missing auth | Same as above. The bundle is HMAC-signed and ADR-009-documented public, but ships the entire compiled coaching library. If rules ever become competitive differentiation, gate behind auth + cache. |
| `organizations.py` | `POST /api/v1/orgs/{org_id}/invites` | LOW | Enumeration | Returns 409 `already_member` *before* the 402 seats check, allowing an org admin to probe arbitrary email addresses to learn whether they are members of the org. Limited blast radius (caller is already org admin), but a B2B privacy concern when admins are not fully trusted (e.g., contractor admins). |
| `organizations.py` | `POST /api/v1/orgs/{org_id}/invites/{token}/accept` | LOW | Status parity | Returns four distinct status codes (404 unknown / 409 already-accepted / 410 expired / 403 email-mismatch). Token is 256-bit so brute-force enumeration is infeasible; an attacker possessing a leaked token can however distinguish "expired" from "consumed by recipient" — minor info disclosure on invitation lifecycle. |
| `pairing.py` | `POST /api/v1/auth/pairing/redeem` | LOW | Defense-in-depth | Public by design (token-based pre-session redemption — pre-JWT). Handler-level rate limit not visible; if not enforced by upstream middleware, the endpoint is a brute-force vector against the token space. T017's rate-limit middleware skeleton exists but per-route configuration was not audited here. |
| `billing.py` | `POST /api/v1/billing/checkout` | LOW | Defense-in-depth | No idempotency or rate-limit at the handler level. A malicious or buggy client could spam Checkout Session creation; cost/Stripe-load nit, not a security issue. |

## What was checked

Every router in `app/routers/` (24 modules) was statically reviewed for:

1. **Missing auth.** Each non-webhook endpoint should depend on
   `Depends(get_current_user)` (or `require_admin`,
   `require_active_subscription`). Webhooks (`webhooks.py`,
   `billing.py /webhooks`) use HMAC verification (svix for Clerk,
   `stripe.Webhook.construct_event` for Stripe). Public-by-design
   exceptions: `catalog.py` (track/car reference data), `pairing.py
   /redeem` (token-redemption pre-session).
2. **IDOR on UUID path params.** session_id, prescription_id,
   weakness_id, insight_id, link_id, case_id, org_id, member_id,
   driver_id, event_id, template_id, device_id all reviewed —
   handlers must load the resource and verify caller ownership
   before mutating or returning it. Clean handlers in
   `compare_cases.py`, `prescriptions.py`, `outcomes.py`,
   `weaknesses.py`, `skills.py`, `sessions.py`, `bridge.py`,
   `target_events.py`, `timeline.py`, `driver_profile.py`,
   `devices.py`, `me.py`, `benchmark.py`, `insights.py`,
   `coaching.py /flag`, `coach.py`, the org subset of
   `organizations.py` (PATCH branding, DELETE org, DELETE member,
   GET members/dashboard/metrics/profile/invitations), and
   `admin.py`.
3. **Privilege escalation.** Org admin-only mutations (org branding,
   member removal, invite creation, org delete) verified to check
   `org.admin_user_id == user.id`. Coach endpoints
   (`coach.py /prescriptions`, `coach.py /sessions/.../coach-notes`)
   verified to reject `caller_membership.role == "driver"`. Admin
   endpoints (`admin.py`) verified to require `user.is_admin`.
4. **Status parity.** 404 vs 403 collapsing reviewed for
   no-existence-leak conventions documented in module docstrings.
   Observed correctly across the listed clean handlers.

## What was NOT checked

Out of scope for this static review (recommend follow-on tasks):

- Rate limiting — middleware skeleton exists per T017 but per-route
  bucket sizing and brute-force coverage not audited. T182 covers this.
- Mass-assignment risk in Pydantic shapes (e.g., would a forged body
  accept server-set fields like `is_admin`, `driver_id`, `created_at`,
  `is_waitlist`?). All existing schemas use `extra="forbid"` per
  convention but not exhaustively reviewed against ORM column sets.
- Race conditions / TOCTOU on concurrent transactions — notably the
  seat-budget computation in `organizations.py /invites` between the
  member count and the invite insert. SQLite tests serialize, but
  Postgres concurrent execution may double-count.
- Log-injection via user-controlled strings reaching `structlog.info`.
  Spot checks looked clean (structured kv, not f-strings) but not
  exhaustive.
- SSRF on outbound calls (Stripe SDK, Posthog client, blob store).
- The actual JWT validation logic in `app/deps.py` —
  `get_current_claims` was not opened: signature, audience, expiry,
  `kid` pinning, leeway, and replay protection should be reviewed
  separately.
- Token TTL/replay on `pairing /redeem`.
- Idempotency-key collision risk in `sessions.ingest`.
- Content-type / file-size / MIME-sniff limits on chunk uploads
  beyond filename traversal.
- CORS / CSRF posture at the middleware layer (T015 covered CORS
  for one origin; full origin set audit pending T184).
- Inner correctness of domain helpers (e.g., `_compute_best_gain`,
  weakness severity ranking, prescription generator) — these are
  business-logic concerns, not auth concerns, but bugs there can
  surface as "wrong driver's data" symptoms.
- Dependency-vulnerability scanning on the Python package set.
- Secret leakage in error messages, exception traces, or 5xx bodies.
- OpenAPI spec accidentally documenting internal-only routes.

## Recommended remediation order

1. **Fix `endurance.py /events/{event_id}` (HIGH)** — add
   `if str(profile.id) not in event.driver_ids: raise 404` after
   loading the driver profile.
2. **Fix `jobs.py /jobs/{job_id}/events` (HIGH)** — extend the job
   bus to register an owning user_id and check it before streaming.
   Pre-existing jobs without ownership data should 403.
3. Confirm with product whether `coaching /rules*` should remain
   public (LOW) — keep as-is or gate behind cheap caching auth.
4. Tighten `organizations /invites` to return a uniform 402
   response that conflates `already_member` and `seats_exhausted`
   when the probing email isn't yet a member, removing the LOW
   enumeration vector.
5. Add per-handler rate limits to `pairing /redeem` and
   `billing /checkout` (LOW defense-in-depth) under T182.

After remediation, recommend a dynamic auth fuzzer pass: forge two
JWTs (driver A and driver B), enumerate every UUID-path-param
endpoint with B's resource ID under A's token, and assert 404 on
all of them. The two HIGH findings would be regression-pinned by
this fuzzer.
