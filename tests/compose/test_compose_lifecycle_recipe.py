"""The convenience recipes provide the host-side gate's database contract."""

from pathlib import Path


def test_lifecycle_recipes_require_role_specific_database_authorities() -> None:
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()
    witness_default = (
        "KDIVE_LIFECYCLE_WITNESS_DATABASE_URL="
        '"${KDIVE_LIFECYCLE_WITNESS_DATABASE_URL:-postgresql://kdive-witness-member:'
        'kdive-witness-local@localhost:${KDIVE_POSTGRES_PORT:-5432}/kdive}"'
    )
    worker_default = (
        'KDIVE_WORKER_DATABASE_URL="${KDIVE_WORKER_DATABASE_URL:-postgresql://'
        'kdive-worker-member:kdive-worker-local@postgres:5432/kdive}"'
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


def test_local_recipes_default_to_allowlisted_distinct_development_members() -> None:
    root = Path(__file__).resolve().parents[2]
    justfile = (root / "justfile").read_text()
    readme = (root / "deploy" / "compose" / "README.md").read_text()

    for member in (
        "kdive-migration",
        "kdive-server-member",
        "kdive-worker-member",
        "kdive-reconciler-member",
        "kdive-witness-member",
    ):
        assert member in justfile or member in readme
    assert "local development only" in readme
    assert "production" in readme
    assert "external" in readme


def test_executable_lifecycle_proof_has_a_dedicated_fail_loud_recipe() -> None:
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()

    assert "test-compose-lifecycle:" in justfile
    assert "KDIVE_RUN_COMPOSE_LIFECYCLE_PROOF=1" in justfile
    assert "KDIVE_REQUIRE_DOCKER=1" in justfile
    assert "tests/compose/test_compose_worker_lifecycle_live.py" in justfile


def test_compose_bootstrap_script_is_covered_by_the_shell_gate() -> None:
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()
    lint_shell = justfile.split("lint-shell:", 1)[1].split("\n\n", 1)[0]

    assert "deploy/compose" in lint_shell
