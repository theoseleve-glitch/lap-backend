# Changelog

All notable changes to lap-backend are documented here.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

The "M-tags" (M1, M2, …) mark internal milestones — `M1` is the
30-day backend sprint covered in `docs/roadmap/sprint-30day/`.

## [Unreleased]

### Added
- _Nothing yet — in-flight work lands here before the next version cuts._

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
