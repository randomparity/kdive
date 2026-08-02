"""The convenience recipes provide the host-side gate's database contract."""

from pathlib import Path


def test_lifecycle_recipes_default_to_the_published_postgres_port() -> None:
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()
    credentials = "kdive:kdive"  # pragma: allowlist secret
    database_default = (
        f'KDIVE_DATABASE_URL="${{KDIVE_DATABASE_URL:-postgresql://{credentials}@localhost:'
        '${KDIVE_POSTGRES_PORT:-5432}/kdive}"'
    )

    assert justfile.count(database_default) == 3
