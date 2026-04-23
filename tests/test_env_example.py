from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
REQUIRED_KEYS = frozenset({"DATABASE_URL", "REDIS_URL", "CLERK_SECRET", "STRIPE_SECRET"})


def _keys_in_env_example() -> set[str]:
    keys: set[str] = set()
    for raw in ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def test_env_example_exists() -> None:
    assert ENV_EXAMPLE.is_file()


def test_env_example_covers_required_config_keys() -> None:
    assert REQUIRED_KEYS <= _keys_in_env_example()
