"""Executable Compose proof for evidence-preserving worker lifecycle (ADR-0533)."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest

from kdive.db.migrate import apply_migrations, discover_migrations
from kdive.processes.lifecycle import compose_worker_lifecycle as lifecycle_module
from kdive.processes.lifecycle.compose_worker_lifecycle import ComposeWorkerLifecycle
from kdive.processes.lifecycle.docker_death_api import WorkerLifecycleGate
from kdive.services.runs.worker_incarnations import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    TerminationOutcome,
    register_worker_incarnation,
    terminate_worker_incarnation,
)

pytestmark = pytest.mark.live_stack

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = Path(__file__).with_name("fixtures") / "worker-lifecycle-live.yml"


def _run(argv: tuple[str, ...], env: dict[str, str], *, timeout: int = 120) -> str:
    return lifecycle_module._bounded_command(
        argv,
        env,
        timeout=timeout,
        max_stdout_bytes=4_194_304,
        max_stderr_bytes=4_194_304,
    )


def _docker_failure(operation: str, exc: subprocess.CalledProcessError) -> RuntimeError:
    detail = str(exc.stderr).strip() or str(exc)
    return RuntimeError(f"Docker {operation} failed: {detail}")


def _is_exact_missing_image(exc: subprocess.CalledProcessError, image: str) -> bool:
    return exc.returncode == 1 and str(exc.stderr).strip() == (
        f"Error response from daemon: No such image: {image}"
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _compose(env: dict[str, str], *args: str, timeout: int = 120) -> str:
    return _run(("docker", "compose", "-f", str(_COMPOSE_FILE), *args), env, timeout=timeout)


def _cutover_postgres_clients(tmp_path: Path) -> Path:
    """Provide host-shaped PostgreSQL clients backed by the isolated fixture container."""
    bin_dir = tmp_path / "cutover-bin"
    bin_dir.mkdir()
    tools = {
        "psql": """shift
exec docker compose exec -T postgres psql -U kdive-migration -d kdive \"$@\"
""",
        "pg_dump": """output=
for argument in \"$@\"; do
  case \"$argument\" in --file=*) output=${argument#--file=} ;; esac
done
[[ -n \"$output\" ]]
docker compose exec -T postgres pg_dump --format=custom -U kdive-migration kdive >\"$output\"
""",
        "pg_restore": """[[ \"${1:-}\" == --list && -f \"${2:-}\" ]]
docker compose exec -T postgres pg_restore --list <\"$2\"
""",
    }
    for name, body in tools.items():
        path = bin_dir / name
        path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


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


def _cleanup_isolated_stack(
    env: dict[str, str], *, project: str, image: str, credential_path: Path
) -> None:
    """Attempt every cleanup arm and prove the unique fixture resources are absent."""
    errors: list[Exception] = []

    try:
        _compose(env, "down", "--volumes", "--remove-orphans", timeout=120)
    except Exception as exc:  # noqa: BLE001 - cleanup must continue through every independent arm
        errors.append(exc)
    try:
        credential_path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - cleanup must continue through every independent arm
        errors.append(exc)
    try:
        _run(("docker", "image", "rm", image), env, timeout=60)
    except subprocess.CalledProcessError as exc:
        if not _is_exact_missing_image(exc, image):
            errors.append(_docker_failure("image removal", exc))
    except Exception as exc:  # noqa: BLE001 - cleanup must continue into the absence probes
        errors.append(exc)

    probes = {
        "containers": (
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ),
        "networks": (
            "docker",
            "network",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ),
        "volumes": (
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ),
    }
    for kind, argv in probes.items():
        try:
            remaining = _run(argv, env, timeout=60).strip()
            if remaining:
                errors.append(RuntimeError(f"isolated Compose {kind} remain: {remaining}"))
        except subprocess.CalledProcessError as exc:
            errors.append(_docker_failure(f"{kind} absence probe", exc))
        except Exception as exc:  # noqa: BLE001 - every postcondition is checked independently
            errors.append(exc)
    try:
        remaining_image = _run(("docker", "image", "inspect", image), env, timeout=60).strip()
        if remaining_image:
            errors.append(RuntimeError(f"isolated Compose image remains: {remaining_image}"))
    except subprocess.CalledProcessError as exc:
        if not _is_exact_missing_image(exc, image):
            errors.append(_docker_failure("image absence probe", exc))
    except Exception as exc:  # noqa: BLE001 - the credential postcondition must still run
        errors.append(exc)
    if credential_path.exists():
        errors.append(RuntimeError(f"isolated Compose credential remains: {credential_path}"))

    if not errors:
        return
    active = sys.exception()
    if active is not None:
        details = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
        active.add_note(f"isolated Compose cleanup failures: {details}")
        return
    raise ExceptionGroup("isolated Compose cleanup failed", errors)


def _apply_migrations_when_stable(admin_dsn: str, *, through: str | None = None) -> None:
    """Cross the official Postgres image's temporary init-server restart."""
    deadline = time.monotonic() + 30
    while True:
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                if through is None:
                    apply_migrations(conn)
                else:
                    with conn.transaction():
                        conn.execute(
                            "CREATE TABLE schema_migrations (version text PRIMARY KEY, "
                            "filename text NOT NULL, checksum text NOT NULL, "
                            "applied_at timestamptz NOT NULL DEFAULT now())"
                        )
                        for migration in discover_migrations():
                            conn.execute(migration.sql.encode())
                            conn.execute(
                                "INSERT INTO schema_migrations (version, filename, checksum) "
                                "VALUES (%s, %s, %s)",
                                (migration.version, migration.filename, migration.checksum),
                            )
                            if migration.version == through:
                                break
            return
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


@contextmanager
def _isolated_stack(
    tmp_path: Path, *, through: str | None = None
) -> Iterator[tuple[dict[str, str], str, str]]:
    token = uuid.uuid4().hex[:12]
    project = f"kdive-lifecycle-proof-{token}"
    image = f"kdive-lifecycle-proof:{token}"
    port = _free_port()
    env = {
        "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_PROOF_IMAGE": image,
        "COMPOSE_FILE": str(_COMPOSE_FILE),
        "KDIVE_IMAGE": image,
        "COMPOSE_PROOF_POSTGRES_PORT": str(port),
        "COMPOSE_PROOF_REPO_ROOT": str(_ROOT),
    }
    credential_path = tmp_path / f"{project}.credential"
    admin_dsn = f"postgresql://kdive-migration:kdive-migration-local@127.0.0.1:{port}/kdive"  # noqa: E501  # pragma: allowlist secret — isolated local proof
    try:
        _run(("docker", "build", "--tag", image, "."), env, timeout=600)
        _compose(env, "up", "-d", "--wait", "--wait-timeout", "60", "postgres")
        _apply_migrations_when_stable(admin_dsn, through=through)
        _compose(env, "--profile", "bootstrap", "run", "--rm", "--no-deps", "role-bootstrap")
        yield env, admin_dsn, str(credential_path)
    finally:
        _cleanup_isolated_stack(env, project=project, image=image, credential_path=credential_path)


def test_isolated_cleanup_preserves_body_failure_and_attempts_every_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"COMPOSE_PROJECT_NAME": "kdive-lifecycle-proof-cleanup"}
    credential_path = tmp_path / "credential"
    credential_path.write_text("secret", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def compose_failure(_env: dict[str, str], *args: str, timeout: int = 120) -> str:
        calls.append(("compose", *args, str(timeout)))
        raise RuntimeError("compose cleanup failed")

    def run_probe(
        argv: tuple[str, ...],
        _env: dict[str, str],
        *,
        timeout: int = 120,
        allow_failure: bool = False,
    ) -> str:
        calls.append((*argv, str(timeout), str(allow_failure)))
        return ""

    monkeypatch.setattr(
        "tests.compose.test_compose_worker_lifecycle_live._compose", compose_failure
    )
    monkeypatch.setattr("tests.compose.test_compose_worker_lifecycle_live._run", run_probe)
    original = ValueError("body failed")
    with pytest.raises(ValueError, match="body failed") as caught:
        try:
            raise original
        finally:
            _cleanup_isolated_stack(
                env,
                project=env["COMPOSE_PROJECT_NAME"],
                image="kdive-lifecycle-proof:cleanup",
                credential_path=credential_path,
            )

    assert caught.value is original
    assert any("compose cleanup failed" in note for note in caught.value.__notes__)
    assert not credential_path.exists()
    assert any(
        call[:4] == ("docker", "image", "rm", "kdive-lifecycle-proof:cleanup") for call in calls
    )
    assert any(call[:3] == ("docker", "network", "ls") for call in calls)
    assert any(call[:3] == ("docker", "volume", "ls") for call in calls)


def test_isolated_cleanup_requires_exact_absence_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"COMPOSE_PROJECT_NAME": "kdive-lifecycle-proof-postcondition"}
    credential_path = tmp_path / "credential"
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        "tests.compose.test_compose_worker_lifecycle_live._compose",
        lambda _env, *args, timeout=120: calls.append(("compose", *args)) or "",
    )

    def run_with_leaked_volume(
        argv: tuple[str, ...],
        _env: dict[str, str],
        *,
        timeout: int = 120,
        allow_failure: bool = False,
    ) -> str:
        calls.append(argv)
        if argv[:3] == ("docker", "volume", "ls"):
            return "kdive-lifecycle-proof-postcondition_proof-postgres\n"
        return ""

    monkeypatch.setattr(
        "tests.compose.test_compose_worker_lifecycle_live._run", run_with_leaked_volume
    )
    with pytest.raises(ExceptionGroup, match="isolated Compose cleanup failed"):
        _cleanup_isolated_stack(
            env,
            project=env["COMPOSE_PROJECT_NAME"],
            image="kdive-lifecycle-proof:postcondition",
            credential_path=credential_path,
        )


def test_cleanup_probe_failures_annotate_body_and_attempt_every_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"COMPOSE_PROJECT_NAME": "kdive-lifecycle-proof-probe-failure"}
    image = "kdive-lifecycle-proof:probe-failure"
    credential_path = tmp_path / "credential"
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        "tests.compose.test_compose_worker_lifecycle_live._compose",
        lambda _env, *args, timeout=120: "",
    )

    def command_failure(
        argv: tuple[str, ...],
        _env: dict[str, str] | None,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> str:
        calls.append(argv)
        raise subprocess.CalledProcessError(
            1,
            argv,
            output="",
            stderr="permission denied while contacting Docker daemon",
        )

    monkeypatch.setattr(lifecycle_module, "_bounded_command", command_failure)
    original = ValueError("body failed")
    with pytest.raises(ValueError, match="body failed") as caught:
        try:
            raise original
        finally:
            _cleanup_isolated_stack(
                env,
                project=env["COMPOSE_PROJECT_NAME"],
                image=image,
                credential_path=credential_path,
            )

    assert caught.value is original
    assert any("Docker" in note and "permission" in note for note in caught.value.__notes__)
    assert calls == [
        ("docker", "image", "rm", image),
        (
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={env['COMPOSE_PROJECT_NAME']}",
        ),
        (
            "docker",
            "network",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={env['COMPOSE_PROJECT_NAME']}",
        ),
        (
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={env['COMPOSE_PROJECT_NAME']}",
        ),
        ("docker", "image", "inspect", image),
    ]


def test_cleanup_accepts_only_exact_image_not_found_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"COMPOSE_PROJECT_NAME": "kdive-lifecycle-proof-missing-image"}
    image = "kdive-lifecycle-proof:missing-image"
    credential_path = tmp_path / "credential"

    monkeypatch.setattr(
        "tests.compose.test_compose_worker_lifecycle_live._compose",
        lambda _env, *args, timeout=120: "",
    )

    def absent_image(
        argv: tuple[str, ...],
        _env: dict[str, str] | None,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> str:
        if argv[:3] == ("docker", "image", "rm") or argv[:3] == (
            "docker",
            "image",
            "inspect",
        ):
            raise subprocess.CalledProcessError(
                1,
                argv,
                output="",
                stderr=f"Error response from daemon: No such image: {image}\n",
            )
        return ""

    monkeypatch.setattr(lifecycle_module, "_bounded_command", absent_image)

    _cleanup_isolated_stack(
        env,
        project=env["COMPOSE_PROJECT_NAME"],
        image=image,
        credential_path=credential_path,
    )


def test_migration_application_retries_postgres_init_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    applied: list[object] = []
    stable_connection = object()

    class ConnectionContext:
        def __enter__(self) -> object:
            return stable_connection

        def __exit__(self, *_args: object) -> None:
            return None

    def connect(_dsn: str, *, autocommit: bool) -> ConnectionContext:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise psycopg.OperationalError("temporary initialization server restarted")
        assert autocommit is True
        return ConnectionContext()

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(
        "tests.compose.test_compose_worker_lifecycle_live.apply_migrations",
        lambda conn: applied.append(conn),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    _apply_migrations_when_stable("postgresql://example")

    assert attempts == 2
    assert applied == [stable_connection]


def _gate(
    project: str,
    witness_dsn: str,
    credential_path: Path,
    *,
    protocol: int = CURRENT_WORKER_FENCE_PROTOCOL,
) -> WorkerLifecycleGate:
    async def register(holder: str, container_id: str, credential_hash: bytes) -> None:
        conn = await psycopg.AsyncConnection.connect(witness_dsn)
        try:
            await register_worker_incarnation(
                conn,
                holder,
                "docker",
                {"container_id": container_id},
                credential_hash,
                protocol,
            )
        finally:
            await conn.close()

    async def terminate(holder: str, binding: dict[str, str], outcome: str) -> None:
        conn = await psycopg.AsyncConnection.connect(witness_dsn)
        try:
            await terminate_worker_incarnation(
                conn, holder, "docker", binding, cast(TerminationOutcome, outcome)
            )
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
    env: dict[str, str],
    witness_dsn: str,
    credential_path: Path,
    *,
    protocol: int = CURRENT_WORKER_FENCE_PROTOCOL,
) -> tuple[ComposeWorkerLifecycle, WorkerLifecycleGate]:
    project = env["COMPOSE_PROJECT_NAME"]
    gate = _gate(project, witness_dsn, credential_path, protocol=protocol)

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


def test_compose_capture_protocol_cutover_uses_exact_termination_and_backup(
    tmp_path: Path,
) -> None:
    if os.environ.get("KDIVE_RUN_COMPOSE_LIFECYCLE_PROOF") != "1":
        pytest.skip("set KDIVE_RUN_COMPOSE_LIFECYCLE_PROOF=1 for the isolated Docker proof")
    with _isolated_stack(tmp_path, through="0111") as (env, admin_dsn, credential_name):
        credential_path = Path(credential_name)
        witness_dsn = (
            admin_dsn.replace(
                "kdive-migration:kdive-migration-local",
                "kdive-witness-member:kdive-witness-local",
            )
            + "?connect_timeout=2"
        )
        lifecycle, _gate_v2 = _managed_lifecycle(env, witness_dsn, credential_path, protocol=2)
        asyncio.run(lifecycle.recreate())
        old_id = _compose(env, "--profile", "managed-worker", "ps", "--all", "-q", "worker").strip()
        old_row = _incarnation_row(admin_dsn, old_id)
        assert old_row is not None
        old_holder = old_row[0]
        job_id = uuid.uuid4()
        with psycopg.connect(admin_dsn) as conn:
            conn.execute(
                "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
                "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
                "(%s, 'capture_traffic', 'running', 1, 3, %s, now(), now(), "
                "'{}'::jsonb, %s)",
                (job_id, old_holder, f"cutover-{job_id}"),
            )

        backup = tmp_path / "pre-protocol-3.dump"
        client_bin = _cutover_postgres_clients(tmp_path)
        cutover_env = {
            **os.environ,
            **env,
            "PATH": f"{client_bin}:{os.environ['PATH']}",
            "KDIVE_DATABASE_URL": admin_dsn,
            "KDIVE_LIFECYCLE_WITNESS_DATABASE_URL": witness_dsn,
            "KDIVE_WORKER_DATABASE_URL": (
                "postgresql://kdive-worker-member:"  # pragma: allowlist secret
                "kdive-worker-local@postgres:5432/kdive"
            ),
            "KDIVE_POSTGRES_PORT": env["COMPOSE_PROOF_POSTGRES_PORT"],
            "KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS": "120",
            "KDIVE_CUTOVER_DB_CONNECT_TIMEOUT_SECONDS": "5",
            "KDIVE_CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS": "60",
            "COMPOSE_PROGRESS": "quiet",
        }
        result = subprocess.run(
            [
                str(_ROOT / "scripts" / "cutover-capture-protocol-compose.sh"),
                str(backup),
                env["KDIVE_IMAGE"],
            ],
            cwd=_ROOT,
            env=cutover_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=360,
        )
        assert result.returncode == 0, result.stderr
        assert _incarnation_row(admin_dsn, old_id) == (old_holder, "terminated", "killed")
        assert backup.stat().st_size > 0
        with psycopg.connect(admin_dsn) as conn:
            assert conn.execute(
                "SELECT protocol, operation_quiescent FROM capture_operation_cutoff"
            ).fetchone() == (3, True)
            assert conn.execute(
                "SELECT state, worker_id, failure_context FROM jobs WHERE id = %s", (job_id,)
            ).fetchone() == (
                "canceled",
                None,
                {"reason": "offline_capture_protocol_cutover"},
            )

        replacement_id = _compose(
            env, "--profile", "managed-worker", "ps", "--all", "-q", "worker"
        ).strip()
        assert replacement_id != old_id
        with psycopg.connect(admin_dsn) as conn:
            assert conn.execute(
                "SELECT fence_protocol, state FROM worker_incarnations "
                "WHERE authority_binding = %s::jsonb",
                (f'{{"container_id":"{replacement_id}"}}',),
            ).fetchone() == (3, "active")
        stopped = subprocess.run(
            ["just", "compose-stop"],
            cwd=_ROOT,
            env=cutover_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        assert stopped.returncode == 0, stopped.stderr
