# lap-backend

FastAPI + Celery + Postgres backend for LAP (Driver OS).

## Sprint queue

This repo's 30-day sprint queue is at:
`C:/Users/Admin/Documents/Telemetry/docs/roadmap/sprint-30day/sprint-backend.md`

## Architecture

See `C:/Users/Admin/Documents/Telemetry/docs/roadmap/02-ARCHITECTURE-DECISIONS.md`. Key ADRs:
- **ADR-001** — FastAPI over Flask
- **ADR-002** — This repo absorbs `lap_tool_virtualisation`
- **ADR-003** — UUIDv7 for all IDs
- **ADR-005** — 7 canonical domain objects
- **ADR-006** — `SessionManifest` shape
- **ADR-007** — Dual-timestamp telemetry samples
- **ADR-009** — Server-authored rules, client-executed
- **ADR-011** — Clerk for auth
- **ADR-012** — Unified `/api/v1/sessions/ingest` endpoint
- **ADR-013** — YAML coaching library format

## Status

Seeded 2026-04-23. Scaffold comes from sprint task T001.

## Stack (per ADR)

- Python 3.12
- FastAPI
- Pydantic v2
- SQLAlchemy 2.0 + Alembic
- Celery 5.4 + Redis
- Postgres 16 (Neon)
- Clerk (auth)
- Stripe (billing)
- Cloudflare R2 (object storage)
- PostHog + Sentry (observability)
