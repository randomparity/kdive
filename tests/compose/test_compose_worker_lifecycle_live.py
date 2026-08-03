"""Executable Compose proof for evidence-preserving worker lifecycle (ADR-0533)."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest

from kdive.db.migrate import apply_migrations
from kdive.processes import compose_worker_lifecycle as lifecycle_module
from kdive.processes.compose_worker_lifecycle import ComposeWorkerLifecycle
from kdive.processes.docker_death_api import WorkerLifecycleGate
from kdive.services.runs.worker_incarnations import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    TerminationOutcome,
    register_worker_incarnation,
    terminate_worker_incarnation,
)

pytestmark = pytest.mark.live_stack

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = Path(__file__).with_name("fixtures") / "worker-lifecycle-live.yml"


def _run(
    argv: tuple[str, ...], env: dict[str, str], *, timeout: int = 120, allow_failure: bool = False
) -> str:
    try:
        return lifecycle_module._bounded_command(
            argv,
            env,
            timeout=timeout,
            max_stdout_bytes=4_194_304,
            max_stderr_bytes=4_194_304,
        )
    except subprocess.CalledProcessError:
        if allow_failure:
            return ""
        raise


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _compose(env: dict[str, str], *args: str, timeout: int = 120) -> str:
    return _run(("docker", "compose", "-f", str(_COMPOSE_FILE), *args), env, timeout=timeout)


def _wait_for_state(container_id: str, expected: str) -> tuple[str, int]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        inspected = lifecycle_module._inspect(container_id)
        if inspected is not None:
            state = inspected["State"]
            if isinstance(state, dict) and state.get("Status") == expected:
                exit_code = state.get("ExitCode")
                assert isinstance(exit_code, int)
                return expected, exit_code
        time.sleep(0.1)
    raise AssertionError(f"container {container_id} did not reach {expected}")


def _incarnation_row(admin_dsn: str, container_id: str) -> tuple[str, str, str] | None:
    with psycopg.connect(admin_dsn) as conn:
        row = conn.execute(
            "SELECT incarnation, state, COALESCE(outcome, '') FROM worker_incarnations "
            "WHERE authority_binding = %s::jsonb",
            (f'{{"container_id":"{container_id}"}}',),
        ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]), str(row[2]))


def _assert_exact_memberships(admin_dsn: str) -> None:
    expected = {
        "kdive-server-member": "kdive_server",
        "kdive-worker-member": "kdive_worker",
        "kdive-reconciler-member": "kdive_reconciler",
        "kdive-witness-member": "kdive_lifecycle_witness",
    }
    with psycopg.connect(admin_dsn) as conn:
        rows = conn.execute(
            "SELECT member.rolname, granted.rolname FROM pg_auth_members membership "
            "JOIN pg_roles member ON member.oid = membership.member "
            "JOIN pg_roles granted ON granted.oid = membership.roleid "
            "WHERE member.rolname = ANY(%s) ORDER BY member.rolname",
            (list(expected),),
        ).fetchall()
    assert {str(member): str(granted) for member, granted in rows} == expected

    for member, capability in expected.items():
        password = {
            "kdive-server-member": "kdive-server-local",
            "kdive-worker-member": "kdive-worker-local",
            "kdive-reconciler-member": "kdive-reconciler-local",
            "kdive-witness-member": "kdive-witness-local",
        }[member]
        member_dsn = admin_dsn.replace(
            "kdive-migration:kdive-migration-local", f"{member}:{password}"
        )
        with psycopg.connect(member_dsn) as member_conn:
            identity = member_conn.execute(
                "SELECT session_user, pg_has_role(session_user, %s, 'MEMBER')", (capability,)
            ).fetchone()
        assert identity == (member, True)


def _assert_runtime_data_access(admin_dsn: str) -> None:
    probes: dict[str, LiteralString] = {
        "kdive-server-member:kdive-server-local": "SELECT count(*) FROM resources",
        "kdive-worker-member:kdive-worker-local": (
            "SELECT queue_paused FROM ops_control WHERE singleton = true"
        ),
        "kdive-reconciler-member:kdive-reconciler-local": "SELECT count(*) FROM allocations",
    }
    for authentication, query in probes.items():
        member_dsn = admin_dsn.replace("kdive-migration:kdive-migration-local", authentication)
        with psycopg.connect(member_dsn) as member_conn:
            assert member_conn.execute(query).fetchone() is not None

    witness_dsn = admin_dsn.replace(
        "kdive-migration:kdive-migration-local",
        "kdive-witness-member:kdive-witness-local",
    )
    with (
        psycopg.connect(witness_dsn) as witness,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        witness.execute("SELECT count(*) FROM resources")


@contextmanager
def _isolated_stack(tmp_path: Path) -> Iterator[tuple[dict[str, str], str, str]]:
    token = uuid.uuid4().hex[:12]
    project = f"kdive-lifecycle-proof-{token}"
    image = f"kdive-lifecycle-proof:{token}"
    port = _free_port()
    env = {
        "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_PROOF_IMAGE": image,
        "COMPOSE_PROOF_POSTGRES_PORT": str(port),
        "COMPOSE_PROOF_REPO_ROOT": str(_ROOT),
    }
    credential_path = tmp_path / f"{project}.credential"
    admin_dsn = f"postgresql://kdive-migration:kdive-migration-local@127.0.0.1:{port}/kdive"  # noqa: E501  # pragma: allowlist secret — isolated local proof
    try:
        _run(("docker", "build", "--tag", image, "."), env, timeout=600)
        _compose(env, "up", "-d", "--wait", "--wait-timeout", "60", "postgres")
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            apply_migrations(conn)
        _compose(env, "--profile", "bootstrap", "run", "--rm", "--no-deps", "role-bootstrap")
        yield env, admin_dsn, str(credential_path)
    finally:
        _compose(env, "down", "--volumes", "--remove-orphans", timeout=120)
        credential_path.unlink(missing_ok=True)
        _run(("docker", "image", "rm", image), env, timeout=60, allow_failure=True)


def _gate(project: str, witness_dsn: str, credential_path: Path) -> WorkerLifecycleGate:
    async def register(holder: str, container_id: str, credential_hash: bytes) -> None:
        conn = await psycopg.AsyncConnection.connect(witness_dsn)
        try:
            await register_worker_incarnation(
                conn,
                holder,
                "docker",
                {"container_id": container_id},
                credential_hash,
                CURRENT_WORKER_FENCE_PROTOCOL,
            )
        finally:
            await conn.close()

    async def terminate(holder: str, outcome: str) -> None:
        conn = await psycopg.AsyncConnection.connect(witness_dsn)
        try:
            await terminate_worker_incarnation(conn, holder, cast(TerminationOutcome, outcome))
        finally:
            await conn.close()

    return WorkerLifecycleGate(
        project=project,
        inspect=lifecycle_module._inspect,
        register=register,
        terminate=terminate,
        credential=lambda: lifecycle_module._credential(credential_path),
        inject=lambda container_id, credential: lifecycle_module._inject_credential(
            credential_path, container_id, credential
        ),
        start=lambda container_id: lifecycle_module._docker_operation("start", container_id),
        stop=lambda container_id: lifecycle_module._docker_operation("stop", container_id),
        remove=lambda container_id: lifecycle_module._docker_operation("rm", container_id),
    )


def _managed_lifecycle(
    env: dict[str, str], witness_dsn: str, credential_path: Path
) -> tuple[ComposeWorkerLifecycle, WorkerLifecycleGate]:
    project = env["COMPOSE_PROJECT_NAME"]
    gate = _gate(project, witness_dsn, credential_path)

    def command(argv: tuple[str, ...], extra_env: dict[str, str] | None) -> str:
        assert argv[:2] == ("docker", "compose")
        return _compose({**env, **(extra_env or {})}, *argv[2:])

    lifecycle = ComposeWorkerLifecycle(
        command=command,
        gate=gate,
        prepare_credential=lambda: lifecycle_module._prepare_credential_file(credential_path),
        credential_retained=lambda: lifecycle_module._credential_retained(credential_path),
        cleanup_credential=lambda: lifecycle_module._cleanup_credential(credential_path),
    )
    return lifecycle, gate


def test_compose_worker_lifecycle_preserves_exact_docker_and_database_evidence(
    tmp_path: Path,
) -> None:
    if os.environ.get("KDIVE_RUN_COMPOSE_LIFECYCLE_PROOF") != "1":
        pytest.skip("set KDIVE_RUN_COMPOSE_LIFECYCLE_PROOF=1 for the isolated Docker proof")
    with _isolated_stack(tmp_path) as (env, admin_dsn, credential_name):
        credential_path = Path(credential_name)
        witness_dsn = (
            admin_dsn.replace(
                "kdive-migration:kdive-migration-local",
                "kdive-witness-member:kdive-witness-local",
            )
            + "?connect_timeout=2"
        )
        lifecycle, gate = _managed_lifecycle(env, witness_dsn, credential_path)
        _assert_exact_memberships(admin_dsn)
        _assert_runtime_data_access(admin_dsn)

        _compose(env, "--profile", "managed-worker", "up", "-d", "worker")
        raw_id = _compose(env, "--profile", "managed-worker", "ps", "--all", "-q", "worker").strip()
        assert _wait_for_state(raw_id, "exited") == ("exited", 1)
        assert _incarnation_row(admin_dsn, raw_id) is None
        _run(("docker", "rm", raw_id), env)

        postgres_id = _compose(env, "ps", "-q", "postgres").strip()
        _run(("docker", "stop", postgres_id), env)
        with pytest.raises(psycopg.OperationalError, match="connect|Connection|refused"):
            asyncio.run(lifecycle.recreate())
        created_id = _compose(
            env, "--profile", "managed-worker", "ps", "--all", "-q", "worker"
        ).strip()
        assert _wait_for_state(created_id, "created") == ("created", 0)
        assert credential_path.stat().st_size == 64

        _run(("docker", "start", postgres_id), env)
        _compose(env, "up", "-d", "--wait", "--wait-timeout", "60", "postgres")
        assert _incarnation_row(admin_dsn, created_id) is None
        asyncio.run(gate.register_and_start(created_id))
        proof = _run(
            (
                "docker",
                "exec",
                created_id,
                "python",
                "-c",
                "import os,stat; p='/run/kdive/worker-incarnation-credential'; "
                "print(f'{os.getuid()}:{len(open(p).read())}:{stat.S_IMODE(os.stat(p).st_mode):o}')",
            ),
            env,
        ).strip()
        assert proof == "10001:64:400"
        created_row = _incarnation_row(admin_dsn, created_id)
        assert created_row is not None
        assert created_row[1:] == ("active", "")

        _run(("docker", "kill", "--signal", "KILL", created_id), env)
        assert _wait_for_state(created_id, "exited") == ("exited", 137)
        asyncio.run(lifecycle.recreate())
        replacement_id = _compose(
            env, "--profile", "managed-worker", "ps", "--all", "-q", "worker"
        ).strip()
        assert replacement_id != created_id
        killed_row = _incarnation_row(admin_dsn, created_id)
        assert killed_row is not None
        assert killed_row[1:] == ("terminated", "killed")
        with pytest.raises(subprocess.CalledProcessError):
            _run(("docker", "inspect", created_id), env)

        _run(("docker", "stop", postgres_id), env)
        with pytest.raises(psycopg.OperationalError, match="connect|Connection|refused"):
            asyncio.run(lifecycle.down())
        assert _wait_for_state(replacement_id, "exited")[0] == "exited"
        assert lifecycle_module._credential_retained(credential_path)
        _run(("docker", "start", postgres_id), env)
        _compose(env, "up", "-d", "--wait", "--wait-timeout", "60", "postgres")
        asyncio.run(lifecycle.down())

        _compose(env, "up", "-d", "--wait", "--wait-timeout", "60", "postgres")
        replacement_row = _incarnation_row(admin_dsn, replacement_id)
        assert replacement_row is not None
        assert replacement_row[1] == "terminated"
        asyncio.run(lifecycle.recreate())
        bypassed_id = _compose(
            env, "--profile", "managed-worker", "ps", "--all", "-q", "worker"
        ).strip()
        _run(("docker", "rm", "--force", bypassed_id), env)
        with pytest.raises(RuntimeError, match="retained worker credential"):
            asyncio.run(lifecycle.down(volumes=True))
