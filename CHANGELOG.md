# Changelog

All notable changes to lap-backend are documented here.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

The "M-tags" (M1, M2, …) mark internal milestones — `M1` is the
30-day backend sprint covered in `docs/roadmap/sprint-30day/`.

## [Unreleased] — M2 (sprint 2)

The second feature sprint expands the backend from "single-driver
coaching pipeline" to "team / cohort / season-aware platform":
cross-session bridge insights, anonymous cohort percentiles,
endurance + strategy modeling, target-event-aware planning, B2B
orgs with coaches, and the compliance / observability surface
required to operate the whole thing in production.

### Added
- **Cross-session bridge (T016–T050).** New `TransferLink` entity
  links two sessions (e.g., two laps of Spa across cars) so a
  driver's transfer learning is a first-class object: lap-aligned
  segmenter, channel extraction (brake/throttle/steering/speed/
  g_lat/g_lon), per-corner cubic-spline normalization, divergence
  scoring, JSONB-indexed divergence_points. CRUD at
  `/api/v1/bridge/links/*` plus `POST /api/v1/bridge/links/:id/rate`
  for usefulness feedback. Bridge RLS pins driver-only visibility;
  same-pair dedup returns 409.
- **Cohort benchmarks (T051–T065).** Opt-in drivers
  (`cohort_opt_in=True` on `DriverProfile`) feed an anonymized
  `cohort_metrics` table — no `driver_id`, only `{car_id, track_id,
  metric, value, session_time}`. Percentile compute requires ≥20
  cohort members for any `{car, track, metric}` slot; below threshold
  the benchmark endpoint returns null. Weekly cron refresh + admin
  cohort-status endpoint + archetype tagging
  (`late_braker`, `smooth_flow`).
- **Endurance + stint scoring (T066–T080).** Endurance event
  representation, stint records, long-run scoring with consistency
  +tire-wear slopes, fuel-delta math, multi-driver attribution.
- **Target events + calendar (T081–T095).** `TargetEvent` entity +
  CRUD at `/api/v1/driver/target-events`. iRacing calendar scraper
  (weekly Monday 08:00 UTC), readiness recompute when target pins,
  prescription-priority reweighting, 24h/1h pre-event email
  notifications, post-mortem feedback loop.
- **Strategy plan vs actual (T096–T110).** `StrategyPlan` +
  `StrategyEvent` entities. Endpoints to set the pre-race plan,
  append runtime events (pit/stint_end/FCY/SC), fetch the combined
  timeline. Plan-vs-actual divergence scoring across pit-lap, fuel,
  stint duration → execution score (0-100). Strategy templates
  (saveable per driver), simulation API ("what if pit +2 laps?").
- **Coaching library expansion (T111–T160).** 50 new rules across
  oversteer/understeer, late-apex/early-apex, brake-balance,
  wet-conditions, qualifying-vs-race, pressure response, night-
  driving, transfer gap. Library now sits at 75 rules.
- **B2B + multi-seat (T161–T180).** `Organization`,
  `OrganizationMember`, `OrganizationInvitation` entities with
  cascade semantics tuned to GDPR. Org create + invite + accept +
  member-remove flow. Multi-seat Stripe with proration on add/remove,
  seat-cap enforcement (402), 30-day trial. Coach role can read
  drivers' profiles but not edit billing. Coach dashboard
  (single-shot org-driver aggregate), coach-assigned prescriptions,
  coach session notes (`coach_session_notes`). Org branding (logo
  + future custom domain).
- **GDPR compliance (T194–T196).** `POST /api/v1/me/export` returns
  a ZIP with `user.json` + `driver_profile.json` + `sessions.json`
  (Article 20 portability). `DELETE /api/v1/me` cascades the
  caller's data, returns 409 if they admin an org (transfer first).
  Weekly Monday 05:00 UTC retention sweep anonymizes opt-out
  drivers' sessions older than 2 years.
- **Admin audit log (T197).** `admin_audit_log` table records
  every mutating admin action (`billing.sync`, `billing.refund`)
  with admin_user_id, action, target_id, structured metadata, and
  server-default timestamp. Append-only by convention; no FK
  cascade so audit history survives user deletion.
- **Observability + alerting (T192–T193).** Prometheus business-
  metric histograms with SLO-bracketed buckets:
  `coaching_pipeline_duration_seconds` (60s budget),
  `next_session_plan_duration_seconds` (5s budget). HTTP request
  counter (`http_requests_total{method, status_class}`) with
  middleware that counts 5xx for unhandled exceptions before
  re-raise. Prometheus alert rule
  `LapBackend5xxBudgetBurn` at `deploy/prometheus/alerts.yml`
  fires on >0.5% sustained 5xx for 10m (matches the 99.5%
  availability SLO).
- **Performance (T190–T191).** N+1 query audit + 6 composite
  indexes covering the hottest dashboard predicates: `weakness
  _episodes(driver_id, status)`, `action_prescriptions(driver_id,
  status)`, `driver_progress_snapshots(driver_id, captured_at)`,
  `sessions(driver_id, created_at)`, `organization_members
  (org_id, role)`, `organization_invitations(org_id, accepted_at,
  expires_at)`.
- **API versioning (T188–T189).** `/api/v2/*` router scaffold
  registered (zero routes — placeholder for v2 work). Deprecation
  policy doc at `docs/api-deprecation-policy.md` defines the v1
  sunset lifecycle (RFC 8594 Sunset header, notice periods).
- **Operational hardening (T181–T186, T200).** Security audit doc
  (`docs/security-audit-2026-04-27.md`), secret rotation runbook,
  CORS tightened to env-overridable allowlist, backup restore
  drill, disaster recovery plan with RTO/RPO + 7 playbooks,
  structured `ErrorCode` enum (47 codes) replacing inline strings.
- **API completion + contract (T001–T015).** Driver timeline
  pagination (opaque base64 cursor), single-shot driver-profile
  aggregate, durable Compare API, weakness-detail endpoint,
  prescription state transitions (`accept`/`skip`/`flag-wrong`),
  skills drilldown, per-session coaching aggregate, insight-flag
  endpoint with rule confidence decrement, nightly false-positive
  aggregation, admin rules-health endpoint, differentiated rate
  limits (read 100/min, write 50/min, ingest 10/h), schemathesis
  contract testing.

### Changed
- Rate-limit middleware split into auth / read / write / ingest
  tiers (T182). Writes get their own bucket so a chatty UI poll
  loop on GETs no longer starves the same user's ability to save
  state.
- `DriverProfile.cohort_opt_in` is now consulted by the cohort
  ingestion job (T052, T055): only opt-in drivers' data flows to
  the anonymized cohort metric store, and opting out purges the
  driver's existing rows (T062).
- `delete_organization` (T176) removes member + invitation rows
  but PRESERVES user / driver_profile / session / coach-notes
  data — orgs are workspaces, not data owners.

### Security
- T181 audit identified two HIGH findings (since fixed) + 5 LOW.
- All admin mutating actions now write to `admin_audit_log` (T197).
- 5xx-rate alert burns the SLO budget loud at 0.5% for 10m
  sustained — paged to PagerDuty before silent degradation has a
  chance to drift the monthly availability number.
- Org-admin self-delete is blocked (409 `cannot_delete_org_admin`)
  so the GDPR delete path can't leave an org with billing alive
  but no admin (T195).

## [0.1.0] — 2026-04-26 — M1 (30-day backend sprint)

The first end-to-end shippable backend: a driver can sign in,
upload a session, get coached on it, and act on prescriptions
that drive the next plan.

### Added
- **Auth & identity (T040–T060).** Clerk JWT verification with JWKS
  caching, `/api/v1/me` driver-self read & profile patch, device
  pairing flow, Clerk webhook signature verification, admin role
  gating.
- **Session ingest pipeline (T080–T120).** Multipart manifest +
  chunk upload at `POST /api/v1/sessions/ingest`, idempotency
  keys, R2 blob storage with sigv4, Celery worker pipeline with
  retry + on-failure markers, SSE job status stream.
- **Coaching engine (T130–T200).** Rules YAML loader, deterministic
  rules evaluator, ML insight generator, signed coaching bundle at
  `GET /api/v1/coaching/rules/bundle`, insight flag endpoint,
  per-driver rolling efficacy stats.
- **Weakness-episode + prescription lifecycle (T210–T223).**
  Episode generation from rule fires, confidence decay over time,
  action prescriptions with accept/skip/expire transitions,
  outcome verdict (met/not_met/inconclusive) with confidence
  propagation, per-driver/per-rule efficacy aggregation.
- **Catalog & profile (T070, T130).** Tracks + cars + layouts
  served from seeded YAML at `/api/v1/catalog/*`, driver profile
  with skill_level + cohort opt-in.
- **Billing (Stripe, T148–T165).** Checkout session creation,
  customer portal link, webhook handling, waitlist promo code
  flow, admin sync + refund tools.
- **Observability (T166–T180).** structlog JSON logging with
  request_id correlation, Sentry FastAPI integration, PostHog
  product events, Prometheus metrics at `/metrics`, weekly KPI
  Slack report, daily session-loss audit.
- **Operational reliability (T200–T240).** Rate limiting middleware,
  CORS lock to `app.lap-coach.racing`, request-ID middleware,
  daily pg_dump → R2 backup at 02:00 UTC, restore-from-R2 CLI,
  pip-audit + bandit security scans in CI, env-backed feature
  flags (`app/flags.py`), `/api/v1` prefix locked with v2
  scaffold ready, error-budget doc at `docs/error-budget.md`,
  k6 load tests for ingest (50 rps, p95 <500ms) and `/me`
  (500 rps, p95 <200ms), OpenAPI spec at `/api/v1/openapi.json`
  with Swagger UI + ReDoc, schemathesis contract test, Dependabot
  + Renovate split-ecosystem dep-update PRs.

### Security
- Bandit + pip-audit gate the CI build (T234). pip CVE-2026-3219
  ignored as build-tooling only.
- Coaching rules bundle requires `COACHING_BUNDLE_SIGNING_SECRET`
  to issue a signed payload — empty secret returns 503 rather
  than ship an unsigned bundle (T204).
- 404 (not 403) returned on cross-driver resource access so an
  attacker cannot enumerate IDs they don't own (T217).
