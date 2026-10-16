"""The convenience recipes provide the host-side gate's database contract."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_LIFECYCLE_MODULE = "python -m kdive.processes.lifecycle.compose.compose_worker_lifecycle"


def _recipe(justfile: str, name: str) -> str:
    return justfile.split(f"{name}:", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]


def test_lifecycle_recipes_require_role_specific_database_authorities() -> None:
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()
    witness_default = (
        'export KDIVE_LIFECYCLE_WITNESS_DATABASE_URL="postgresql://kdive-witness-member:'
        'kdive-witness-local@${endpoint}/kdive"'
    )
    for name in ("compose-up", "compose-stop", "compose-recreate-worker", "compose-down"):
        recipe = _recipe(justfile, name)
        assert 'if [[ -z "${KDIVE_LIFECYCLE_WITNESS_DATABASE_URL:-}" ]]' in recipe
        assert "backend-endpoint.sh KDIVE_POSTGRES_PORT" in recipe
        assert recipe.index("endpoint=") < recipe.index(witness_default)
    worker_default = (
        'KDIVE_WORKER_DATABASE_URL="${KDIVE_WORKER_DATABASE_URL:-postgresql://'
        'kdive-worker-member:kdive-worker-local@postgres:5432/kdive}"'
    )

    assert justfile.count(witness_default) == 4
    assert justfile.count(worker_default) == 2
    assert justfile.count(_LIFECYCLE_MODULE) == 4
    assert _LIFECYCLE_MODULE.replace(".lifecycle", "") not in justfile


def test_compose_stop_preserves_volumes_while_compose_down_removes_them() -> None:
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()
    witness_default = "KDIVE_LIFECYCLE_WITNESS_DATABASE_URL="

    assert "compose-stop:" in justfile
    stop_recipe = _recipe(justfile, "compose-stop")
    down_recipe = _recipe(justfile, "compose-down")

    assert witness_default in stop_recipe
    assert witness_default in down_recipe
    assert f"{_LIFECYCLE_MODULE} down" in stop_recipe
    assert "--volumes" not in stop_recipe
    assert "--volumes" in down_recipe


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
    assert "command -v bwrap" in _recipe(justfile, "test-compose-lifecycle")


def test_live_runner_provisions_and_verifies_bubblewrap() -> None:
    root = Path(__file__).resolve().parents[2]
    defaults = (root / "deploy/ansible/roles/local_worker_host/defaults/main.yml").read_text()
    verify = (root / "deploy/ansible/roles/live_vm_host/tasks/verify.yml").read_text()

    assert "bubblewrap" in defaults
    assert "bwrap --version" in verify


def test_volume_persistence_proof_has_a_dedicated_fail_loud_recipe() -> None:
    # `just test-live-stack` treats a skip as success, so the sole carrier of the ADR-0552
    # runtime evidence needs its own recipe whose environment makes absent Docker a failure.
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()
    recipe = _recipe(justfile, "test-compose-volumes")

    assert "KDIVE_RUN_COMPOSE_VOLUME_PROOF=1" in recipe
    assert "KDIVE_REQUIRE_DOCKER=1" in recipe
    assert "tests/compose/test_compose_volume_persistence_live.py" in recipe


def test_compose_bootstrap_script_is_covered_by_the_shell_gate() -> None:
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()
    lint_shell = justfile.split("lint-shell:", 1)[1].split("\n\n", 1)[0]

    assert "deploy/compose" in lint_shell


def test_examples_scripts_are_covered_by_the_shell_gate() -> None:
    justfile = (Path(__file__).resolve().parents[2] / "justfile").read_text()
    lint_shell = justfile.split("lint-shell:", 1)[1].split("\n\n", 1)[0]

    assert "examples" in lint_shell


@pytest.mark.skipif(shutil.which("just") is None, reason="just is required to run the recipes")
@pytest.mark.parametrize(
    "recipe", ["compose-up", "compose-stop", "compose-recreate-worker", "compose-down"]
)
@pytest.mark.parametrize(
    ("publish", "endpoint", "witness_override"),
    [
        (None, "localhost:5432", None),
        ("17779", "localhost:17779", None),
        ("17779", "localhost:17779", ""),
        ("127.0.0.1:17779", "localhost:17779", None),
        ("0.0.0.0:17779", "localhost:17779", None),
        ("192.0.2.10:17779", "192.0.2.10:17779", None),
        ("[::1]:17779", "localhost:17779", None),
        ("[2001:db8::1]:17779", "[2001:db8::1]:17779", None),
        ("bad", None, None),
        ("bad", None, "postgresql://operator.example.test/kdive"),
    ],
)
def test_lifecycle_recipe_derives_witness_endpoint_from_publish_value(
    tmp_path: Path,
    recipe: str,
    publish: str | None,
    endpoint: str | None,
    witness_override: str | None,
) -> None:
    root = Path(__file__).resolve().parents[2]
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\n" "$KDIVE_LIFECYCLE_WITNESS_DATABASE_URL" '
        '"${KDIVE_WORKER_DATABASE_URL:-}"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KDIVE_", "AWS_"))}
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["KDIVE_LIBVIRT_URI"] = "qemu:///system"
    if witness_override is not None:
        env["KDIVE_LIFECYCLE_WITNESS_DATABASE_URL"] = witness_override
        env["KDIVE_WORKER_DATABASE_URL"] = "postgresql://worker.example.test/kdive"
    if publish is None:
        command = 'source scripts/live-stack/env.sh && just "$1"'
    else:
        env["KDIVE_POSTGRES_PORT"] = publish
        command = 'just "$1"'
    result = subprocess.run(
        ["bash", "-c", command, "bash", recipe],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if endpoint is None and witness_override is None:
        assert result.returncode != 0
        assert result.stdout == ""
        assert "must be PORT or ADDR:PORT" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    witness, worker = result.stdout.splitlines()
    if witness_override:
        assert witness == witness_override
    else:
        assert witness.endswith(f"@{endpoint}/kdive")
    if recipe in {"compose-up", "compose-recreate-worker"}:
        if witness_override is not None:
            assert worker == "postgresql://worker.example.test/kdive"
        else:
            worker_endpoint = "localhost:5432" if publish is None else "postgres:5432"
            assert worker.endswith(f"@{worker_endpoint}/kdive")
