# CLAUDE.md — lap-backend

Repo-scoped instructions. Extends `C:/Users/Admin/CLAUDE.md`.

## Context
- Stack: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, Celery, Postgres, Redis.
- Sprint queue: `../docs/roadmap/sprint-30day/sprint-backend.md`.
- ADRs: `../docs/roadmap/02-ARCHITECTURE-DECISIONS.md` (treat as binding).
- Doctrine: server owns the brain, clients thin.

## Conventions
- Layout: `app/` (routers), `domain/` (pure business logic), `infra/` (DB + external), `coaching/` (library + rules engine), `tests/`.
- Every file ≤500 LOC.
- Every public function typed with strict mypy.
- Every new endpoint has a Pydantic request + response model + a test.
- Every new DB column has an Alembic migration.
- Every new Pydantic model in `lap-schemas` (the shared schemas package), NOT in this repo.
- UUIDv7 for all IDs.
- UTC RFC3339 for all timestamps.

## Forbidden
- No raw SQL outside `infra/`.
- No business logic in routers (put in `domain/`).
- No client-visible secret logging.
- No `print()` — use `structlog`.
- No skipping tests with `@pytest.mark.skip` without a written reason.
- No `--no-verify` on commits.

## /loop rules
- Read sprint file, pick next `[ ]`, complete fully, commit, mark `[x]` with sha.
- If blocked, mark `[BLOCKED: <reason>]` and skip to next.
- Invoke superpowers:test-driven-development for all new code.
- Invoke superpowers:verification-before-completion before claiming done.
- Minimal diffs — stay in task scope.
