"""The convenience recipes provide the host-side gate's database contract."""

from pathlib import Path


def test_lifecycle_recipes_require_role_specific_database_authorities() -> None:
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()
    witness_default = (
        "KDIVE_LIFECYCLE_WITNESS_DATABASE_URL="
        '"${KDIVE_LIFECYCLE_WITNESS_DATABASE_URL:?set the lifecycle-witness DSN}"'
    )
    worker_default = (
        'KDIVE_WORKER_DATABASE_URL="${KDIVE_WORKER_DATABASE_URL:?set the worker-role DSN}"'
    )

    assert justfile.count(witness_default) == 3
    assert justfile.count(worker_default) == 2


def test_documented_recipes_do_not_expose_raw_worker_lifecycle() -> None:
    root = Path(__file__).resolve().parents[2]
    justfile = (root / "justfile").read_text()
    readme = (root / "deploy" / "compose" / "README.md").read_text()

    assert "docker compose up worker" not in justfile
    assert "docker compose restart worker" not in justfile
    assert "docker compose rm worker" not in justfile
    assert "docker compose down -v" not in readme
    assert "just compose-down" in readme
