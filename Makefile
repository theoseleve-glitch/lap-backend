.PHONY: dev test lint migrate

dev:
	uv run uvicorn app.main:app --reload

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run mypy

migrate:
	@echo "alembic migrations: wired up in T057"
