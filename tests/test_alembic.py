from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig

from alembic import command

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def test_alembic_upgrade_head_on_empty_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No migrations exist yet; this must still succeed as a no-op.
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)

    cfg = AlembicConfig(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")


def test_alembic_ini_script_location_points_at_alembic_dir() -> None:
    cfg = AlembicConfig(str(ALEMBIC_INI))
    loc = cfg.get_main_option("script_location")
    assert loc is not None
    assert "alembic" in loc
