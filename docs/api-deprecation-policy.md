# API Deprecation Policy (T189)

How v1 endpoints sunset once v2 (or later) replacements ship.
Companion to the v2 scaffold (T188), `error-budget.md`, and
`disaster-recovery.md`.

## Doctrine

**v1 is the primary surface as of M1.** No client should be
forced to v2 before there is a concrete reason — we don't break
working code for aesthetic reasons. A v1 endpoint stays alive
for as long as it works correctly and meets the SLOs in
`error-budget.md`.

**v2 endpoints ship additively.** A new v2 endpoint never
implicitly retires its v1 sibling. The retirement is a
**separate, deliberate decision** with its own announcement,
its own grace period, and its own runbook entry.

## When to deprecate a v1 endpoint

Deprecate (mark for sunset) only when at least one of these is
true:

1. **Wire-shape change required.** A v2 endpoint exists with a
   different request/response shape that the v1 endpoint cannot
   serve without a backwards-incompat edit.
2. **Security defect.** The v1 endpoint has an inherent design
   flaw (e.g., IDOR — see `security-audit-2026-04-27.md`) that
   cannot be fixed without breaking the contract.
3. **Cost / load disproportionate.** The v1 endpoint dominates
   infra cost and a meaningfully cheaper v2 exists. Document the
   cost delta in the deprecation notice.
4. **Compliance / legal.** Regulators or contracts require a
   contract change.

Cosmetic preferences ("v2 has nicer field names") are NOT
sufficient. If the only argument is naming, keep v1 alive.

## Notice periods

| Audience class | Minimum notice before sunset |
|----------------|------------------------------|
| **B2B contractual customers** | 180 days (or contract terms — whichever is longer) |
| **Consumer (web + mobile)** | 90 days |
| **Internal callers (our own clients)** | 30 days, gated on every internal client having shipped a v2-using build |
| **Catalog / public-read endpoints** | 60 days |

The notice period is from the **public deprecation announcement**
to the **sunset date** — not from the day v2 first shipped.

## Lifecycle stages

Each v1 endpoint moves through these states:

```
active → deprecated → sunset (410 Gone)
```

- **active** — default; no special headers; SLOs apply per
  `error-budget.md`.
- **deprecated** — endpoint still returns 2xx, but every response
  carries the headers below. Clients SHOULD migrate. SLOs still
  apply (deprecation is not an excuse to let perf rot).
- **sunset** — endpoint returns `410 Gone` with a body pointing
  at the v2 replacement. SLOs no longer apply (it's effectively
  decommissioned).

Returning anything other than `410` from a sunset endpoint is a
contract violation — the client is owed a clear "this is over"
signal so it stops retrying.

## Response headers during the deprecated stage

Every response from a deprecated v1 endpoint includes both
RFC 8594 (`Sunset`) and the proposed `Deprecation` header:

```
Deprecation: true
Sunset: Sat, 31 Oct 2026 23:59:59 GMT
Link: </api/v2/sessions/{id}>; rel="successor-version"
```

- `Deprecation: true` — fixed string. The proposed
  draft-ietf-httpapi-deprecation-header allows a date here, but
  `true` is the safe interop value most clients understand.
- `Sunset: <HTTP-date>` — RFC 8594. The exact moment the
  endpoint will start returning 410. Always UTC.
- `Link: <new-url>; rel="successor-version"` — the v2 path
  clients should migrate to. If there is no 1:1 successor, omit
  the Link header rather than lying about a substitute.

These are added by a small middleware when a route is in the
deprecated registry — a per-route `responses=` won't work because
the headers must show on every status, not just 200.

## Body change during the deprecated stage

Do NOT add a `deprecation_warning` field to response bodies. The
body schema is what clients pin to, and adding a field changes
that contract. The headers carry the signal; the body is
unchanged.

## Sunset day procedure

The day a deprecated endpoint becomes sunset:

1. **Replace the handler** with a 410 stub. Add a fresh
   `ErrorCode.ENDPOINT_SUNSET` (or per-endpoint code like
   `ErrorCode.SESSIONS_GET_V1_SUNSET` — let the existing enum
   naming pattern guide you) to `app/error_codes.py`, then in
   the handler:
   ```python
   raise HTTPException(
       status_code=410,
       detail=ErrorCode.ENDPOINT_SUNSET,
   )
   ```
   The handler stays in the routing table at the original path —
   it doesn't disappear. Clients still hitting `/api/v1/foo` get
   a clean 410 with the same `Sunset` header continuing to point
   at the v2 successor. The `app/error_codes.py` test will fail
   if the enum isn't updated alongside the handler edit, which is
   the desired forcing function.
2. **Add a migration entry** to `docs/api-deprecation-log.md`
   (create if missing): date sunset, original path, replacement,
   reason. This becomes the source of truth for "why is this
   endpoint gone" questions years from now.
3. **Update the OpenAPI schema** so v2 readers don't see the
   sunset endpoint listed under v1. The endpoint stays callable
   (returning 410), but the docs reflect the post-sunset reality.
4. **Notify Sentry / dashboards** so the residual 4xx traffic on
   that path is filtered from the error budget.

## Communication plan

| Audience | Channel | When |
|----------|---------|------|
| **B2B customers** | Email + status page banner + account-manager outreach | Day 0 of notice + 30/60/90 days before sunset |
| **Consumer web/mobile** | In-app banner + release notes + status page | Day 0 + 30 days before sunset |
| **Internal team** | Slack #engineering + this runbook entry | Day 0 |
| **Public docs** | OpenAPI schema `deprecated: true` + a callout in the relevant resource page | Day 0 |

For B2B, the email must include: the deprecation rationale, the
replacement v2 path, a worked diff showing the request/response
delta, and the sunset date in their local timezone.

## Worked example (hypothetical)

Say `GET /api/v1/sessions/{id}` (heavy-read, returns
`SessionDetail`) is replaced by `GET /api/v2/sessions/{id}` which
returns a richer shape including session-level coaching insights
inline.

**Day 0 — deprecation announced.** Sunset date: 2026-09-30.
Middleware added that emits the three headers on every
`/api/v1/sessions/{id}` response. OpenAPI schema marks the v1
path `deprecated: true`. Email sent to B2B customers.

**Day 30 / 60 / 90.** Reminder emails. PostHog dashboard tracks
the migration: percentage of customers still on v1.

**Day 90 → sunset.** v1 handler replaced with a 410 stub. Sentry
filter added so residual 4xx on this path doesn't burn the error
budget. Sunset entry added to `docs/api-deprecation-log.md`.

## What this policy does NOT cover

- Breaking changes WITHIN v1 — those should never happen. v1 is
  contract-locked. If a v1 endpoint behaves wrongly, fix the
  behavior to match its current schema; if the schema is wrong,
  the answer is a v2 endpoint, not a silent v1 edit.
- Field-level deprecations (deprecating a single response field
  without retiring the endpoint). v1 does not support this; if
  a field is wrong, ship a v2 endpoint.
- API key / OAuth scope deprecation — handled by the auth
  provider's own runbook (see `secret-rotation.md`).
- Mobile app version sunset — out of scope; the mobile team's
  own deprecation policy handles that.

## Pre-deprecation checklist

Before announcing a v1 endpoint deprecation, the engineer
proposing it must:

- [ ] Confirm a v2 successor exists and is in `active` state
- [ ] Quantify usage of the v1 endpoint (PostHog or DB-side
      log query) so the deprecation impact is known
- [ ] Identify every internal caller (search the
      lap-frontend, lap-mobile, and lap-desktop repos for the
      path) and confirm they have a migration plan
- [ ] Pick a sunset date that respects the longest applicable
      notice period from the table above
- [ ] Pre-write the announcement email + in-app banner copy
- [ ] Prepare the middleware-registry edit so the headers fire
      on day 0
- [ ] Get sign-off from at least one other engineer + product
