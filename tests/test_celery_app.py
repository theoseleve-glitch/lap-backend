"""T107 — Celery worker app starts clean.

The CLI invocation is:
    celery -A infra.celery_app:celery_app worker -l info

"Starts clean" means: the module imports without error, the Celery
instance is correctly configured (name + broker), and the worker CLI
parses options without dying on the `-A` import.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from celery import Celery

from infra.celery_app import celery_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_celery_app_is_a_celery_instance() -> None:
    assert isinstance(celery_app, Celery)


def test_celery_app_main_is_lap_backend() -> None:
    assert celery_app.main == "lap-backend"


def test_broker_url_taken_from_redis_url_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The factory should re-read REDIS_URL each time it's invoked. We
    # call build_celery_app() directly so the cached singleton doesn't
    # mask configuration regressions.
    from infra.celery_app import build_celery_app

    monkeypatch.setenv("REDIS_URL", "redis://broker.test:6379/2")
    app = build_celery_app()
    assert app.conf.broker_url == "redis://broker.test:6379/2"


def test_result_backend_taken_from_redis_url_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from infra.celery_app import build_celery_app

    monkeypatch.setenv("REDIS_URL", "redis://broker.test:6379/3")
    app = build_celery_app()
    assert app.conf.result_backend == "redis://broker.test:6379/3"


def test_celery_worker_help_loads_app_and_exits_zero() -> None:
    # `worker --help` exercises the -A import path without connecting
    # to a broker. A non-zero exit means the module raised during
    # import — i.e. the worker would not start clean in production.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "infra.celery_app:celery_app",
            "worker",
            "--help",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"celery worker --help failed:\nstdout={proc.stdout}\n"
        f"stderr={proc.stderr}"
    )
