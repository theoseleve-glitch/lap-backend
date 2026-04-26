# Error Budget — lap-backend

This is the perf and availability contract for the lap-backend API.
The numbers here are the same numbers asserted by the k6 load tests
(`scripts/k6/`) and by the architecture decisions in
`../../docs/roadmap/02-ARCHITECTURE-DECISIONS.md`. If you change a
number here, change it everywhere.

## Why this exists

An error budget makes the trade-off explicit: "we will tolerate this
much unreliability in exchange for shipping speed." Without it,
every incident becomes a debate about whether to slow down. With it,
the debate is settled by math: budget burned ⇒ feature freeze, budget
intact ⇒ keep shipping.

## Availability target

**99.5% monthly** for all `/api/v1/*` HTTP endpoints.

That is ~3h 39m of allowed unavailability per 30-day month. We pick
99.5 (not 99.9) deliberately for M1 — a single-region Postgres + Redis
deployment cannot honestly promise three nines, and a budget you cannot
hit is no budget at all. Revisit at M2 when multi-region is on the table.

Webhooks (Stripe, Clerk) and `/healthz`/`/readyz`/`/metrics` are
excluded — they have their own targets below.

## Latency SLO classes

Endpoints are bucketed by class. New routes inherit the class SLO
without needing a per-route entry.

| Class            | p95 latency budget | Examples                                       |
|------------------|---------------------|------------------------------------------------|
| **cheap-read**   | **200ms**           | `GET /api/v1/me`, `GET /api/v1/catalog/tracks` |
| **cheap-write**  | 200ms               | `POST /api/v1/prescriptions/{id}/accept`       |
| **heavy-read**   | 500ms               | `GET /api/v1/sessions/{id}`, list/aggregate    |
| **write/upload** | **500ms**           | `POST /api/v1/sessions/ingest`                 |
| **webhook**      | 1000ms              | `POST /api/v1/billing/webhooks` (Stripe)       |
| **admin**        | 2000ms              | `POST /api/v1/admin/billing/refund` (low rps)  |
| **health**       | 50ms                | `GET /healthz`, `GET /readyz`                  |

Bolded rows are the two classes covered by k6 load tests.

## Per-endpoint mapping

### Cheap-read (p95 <200ms)
- `GET /api/v1/me` — driver self read **(load tested: T231 @ 500 rps)**
- `GET /api/v1/catalog/tracks`, `GET /api/v1/catalog/cars`
- `GET /api/v1/coaching/rules`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/auth/devices`

### Cheap-write (p95 <200ms)
- `POST /api/v1/prescriptions/{id}/accept`
- `POST /api/v1/prescriptions/{id}/skip`
- `POST /api/v1/coaching/insights/{id}/flag`
- `POST /api/v1/auth/pairing/create`
- `POST /api/v1/auth/pairing/redeem`
- `DELETE /api/v1/auth/devices/{id}`
- `PATCH /api/v1/me`

### Heavy-read (p95 <500ms)
- `GET /api/v1/sessions/{id}`
- `GET /api/v1/sessions/{id}/next-actions`
- `GET /api/v1/driver/{id}/prescriptions`
- `GET /api/v1/driver/{id}/outcomes`
- `GET /api/v1/coaching/rules/bundle`

### Write/upload (p95 <500ms)
- `POST /api/v1/sessions/ingest` **(load tested: T230 @ 50 rps)**

### Webhook (p95 <1000ms)
- `POST /api/v1/billing/webhooks` — Stripe
- `POST /api/v1/auth/webhooks` — Clerk

### Billing (third-party calls — heavier than cheap-read)
- `POST /api/v1/billing/checkout` — heavy-read class (calls Stripe)
- `GET /api/v1/billing/portal` — heavy-read class (calls Stripe)

### Admin (low traffic — p95 <2000ms)
- All `/api/v1/admin/*` routes

## Pipeline budgets (end-to-end, not HTTP)

These come from `docs/roadmap/02-ARCHITECTURE-DECISIONS.md`:

- **Post-session coaching**: p95 <60s after session upload completes.
  Measured from ingest accept → coaching insights persisted.
- **Next-session plan**: p95 <5s. Deterministic rules + cached ML
  predictions.

These are user-visible budgets — when a driver finishes a session, the
phone polls until coaching is ready. 60s is the perceived "fast" line.

## Where the load tests live

- `scripts/k6/sessions_ingest.js` — write/upload class verifier
- `scripts/k6/driver_profile.js` — cheap-read class verifier

Run via `k6 run -e BASE_URL=... -e BEARER=... <script>`.

## Burning the budget

If 30-day rolling availability falls below 99.5%, or if any class p95
exceeds its SLO for >24h, the response is:

1. **Feature freeze** for the affected service. New endpoints/changes
   block until the regression is identified and reverted or fixed.
2. **Postmortem** within 5 business days, written and linked from this
   doc's git history (no separate tracker — keep the artifact close
   to the budget it relates to).
3. **Revisit the SLO** if the burn is structural (the class is
   unrealistic given the architecture). Loosening an SLO is allowed
   but must be paired with the load-test edit and a note in this
   section's git log.

The point is to make the trade-off legible. A burned budget is not a
crisis — it is data.
