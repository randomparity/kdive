"""Evidence-preserving worker lifecycle for the reference Compose deployment."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import io
import json
import os
import re
import secrets
import stat
import subprocess
import tarfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

import psycopg

from kdive.config import require
from kdive.config.core_settings import (
    LIFECYCLE_WITNESS_DATABASE_URL,
    WORKER_DATABASE_URL,
)
from kdive.processes.docker_death_api import WorkerLifecycleGate
from kdive.services.runs.worker_incarnations import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    register_worker_incarnation,
    terminate_worker_incarnation,
)

type Command = Callable[[tuple[str, ...], dict[str, str] | None], str]
type PrepareCredential = Callable[[], None]
type CreateEnvironment = Callable[[], dict[str, str]]
type CredentialRetained = Callable[[], bool]
type CleanupCredential = Callable[[], None]

_COMPOSE = ("docker", "compose")
_PROFILE = ("--profile", "managed-worker")
_PROJECT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}")
_FULL_ID = re.compile(r"[0-9a-f]{64}")
_CREDENTIAL = re.compile(r"[0-9a-f]{64}")


class LifecycleGate(Protocol):
    """Exact worker gate operations used by the Compose supervisor."""

    async def register_and_start(self, container_id: str) -> None: ...

    async def terminate_and_remove(self, container_id: str) -> None: ...

    async def reconcile(self, container_id: str) -> bool: ...


class ComposeWorkerLifecycle:
    """Order reference-stack operations around one exact managed worker."""

    def __init__(
        self,
        *,
        command: Command,
        gate: LifecycleGate,
        nonce: Callable[[], str] = lambda: secrets.token_hex(16),
        prepare_credential: PrepareCredential = lambda: None,
        create_environment: CreateEnvironment = dict,
        credential_retained: CredentialRetained = lambda: False,
        cleanup_credential: CleanupCredential = lambda: None,
    ) -> None:
        self._command = command
        self._gate = gate
        self._nonce = nonce
        self._prepare_credential = prepare_credential
        self._create_environment = create_environment
        self._credential_retained = credential_retained
        self._cleanup_credential = cleanup_credential

    def _worker_id(self) -> str | None:
        value = self._command((*_COMPOSE, *_PROFILE, "ps", "--all", "-q", "worker"), None).strip()
        return value or None

    def _require_safe_absence(self) -> None:
        if self._credential_retained():
            raise RuntimeError(
                "managed worker is absent but its retained worker credential remains"
            )

    async def _create(self) -> None:
        self._prepare_credential()
        nonce = self._nonce()
        self._command(
            (*_COMPOSE, *_PROFILE, "create", "--no-recreate", "worker"),
            {
                "KDIVE_WORKER_INCARNATION_NONCE": nonce,
                **self._create_environment(),
            },
        )
        container_id = self._worker_id()
        if container_id is None:
            raise RuntimeError("Compose did not retain the created worker container")
        await self._gate.register_and_start(container_id)

    async def up(self) -> None:
        """Start the non-worker graph, then create, bind, and start the worker."""
        self._command((*_COMPOSE, "up", "-d", "--wait", "--wait-timeout", "120"), None)
        current = self._worker_id()
        if current is None:
            self._require_safe_absence()
            await self._create()
        elif await self._gate.reconcile(current):
            self._cleanup_credential()
            await self._create()

    async def recreate(self) -> None:
        """Terminate the old generation before creating its replacement."""
        current = self._worker_id()
        if current is not None:
            await self._gate.terminate_and_remove(current)
            self._cleanup_credential()
        else:
            self._require_safe_absence()
        await self._create()

    async def down(self, *, volumes: bool = False) -> None:
        """Record worker termination before removing the database and gate."""
        current = self._worker_id()
        if current is not None:
            await self._gate.terminate_and_remove(current)
            self._cleanup_credential()
        else:
            self._require_safe_absence()
        args = (*_COMPOSE, "down", *(("--volumes",) if volumes else ()), "--remove-orphans")
        self._command(args, None)


def _command(argv: tuple[str, ...], extra_env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(extra_env or {})},
    )
    return result.stdout


def _inspect(container_id: str) -> Mapping[str, object] | None:
    result = _command(("docker", "inspect", container_id), None)
    decoded = json.loads(result)
    if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
        return None
    return decoded[0]


async def _docker_operation(operation: str, container_id: str) -> None:
    _command(("docker", operation, container_id), None)


async def _register(holder: str, container_id: str, credential_hash: bytes) -> None:
    conn = await psycopg.AsyncConnection.connect(require(LIFECYCLE_WITNESS_DATABASE_URL))
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


async def _terminate(holder: str, outcome: str) -> None:
    if outcome not in {"succeeded", "failed", "killed"}:
        raise RuntimeError("Docker lifecycle produced an unsupported termination outcome")
    conn = await psycopg.AsyncConnection.connect(require(LIFECYCLE_WITNESS_DATABASE_URL))
    try:
        await terminate_worker_incarnation(conn, holder, outcome)  # type: ignore[arg-type]
    finally:
        await conn.close()


def _credential_path(project: str) -> Path:
    return Path(f"/tmp/kdive-compose-worker-{project}.credential")


def _prepare_credential_file(path: Path) -> None:
    descriptor = _open_credential_file(path, os.O_CREAT | os.O_WRONLY)
    try:
        os.ftruncate(descriptor, 0)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _open_credential_file(path: Path, flags: int) -> int:
    try:
        descriptor = os.open(path, flags | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise RuntimeError("worker credential handoff is not a safe supervisor-owned file") from exc
    metadata = os.fstat(descriptor)
    if (
        metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or not stat.S_ISREG(metadata.st_mode)
    ):
        os.close(descriptor)
        raise RuntimeError("worker credential handoff is not a safe supervisor-owned file")
    return descriptor


def _credential(path: Path) -> str:
    descriptor = _open_credential_file(path, os.O_RDONLY)
    with os.fdopen(descriptor, encoding="utf-8") as handoff:
        value = handoff.read().strip()
    generated = not value
    if not value:
        value = secrets.token_hex(32)
    if not _CREDENTIAL.fullmatch(value):
        raise RuntimeError("retained worker credential is not a 256-bit lowercase hex value")
    if generated:
        _write_credential(path, value)
    return value


def _credential_retained(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except FileNotFoundError:
        return False


def _cleanup_credential(path: Path) -> None:
    path.unlink(missing_ok=True)


def _write_credential(path: Path, credential: str) -> None:
    descriptor = _open_credential_file(path, os.O_WRONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handoff:
        os.fchmod(handoff.fileno(), 0o600)
        handoff.truncate(0)
        handoff.write(credential)
        handoff.flush()
        os.fsync(handoff.fileno())


async def _inject_credential(path: Path, container_id: str, credential: str) -> None:
    if not _FULL_ID.fullmatch(container_id) or not _CREDENTIAL.fullmatch(credential):
        raise RuntimeError("worker credential injection requires exact bounded inputs")
    _write_credential(path, credential)
    try:
        result = subprocess.run(
            ("docker", "cp", "--archive", "-", f"{container_id}:/run"),
            input=_credential_archive(credential),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Docker credential injection failed within its 30-second bound") from exc
    if result.returncode != 0:
        raise RuntimeError("Docker rejected the bounded worker credential injection")


def _credential_archive(credential: str) -> bytes:
    if not _CREDENTIAL.fullmatch(credential):
        raise RuntimeError("worker credential archive requires a 256-bit lowercase hex value")
    data = credential.encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        directory = tarfile.TarInfo("kdive")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o700
        directory.uid = 10001
        directory.gid = 10001
        directory.mtime = 0
        archive.addfile(directory)

        handoff = tarfile.TarInfo("kdive/worker-incarnation-credential")
        handoff.size = len(data)
        handoff.mode = 0o400
        handoff.uid = 10001
        handoff.gid = 10001
        handoff.mtime = 0
        archive.addfile(handoff, io.BytesIO(data))
    return buffer.getvalue()


def _lifecycle(project: str) -> ComposeWorkerLifecycle:
    credential_path = _credential_path(project)
    gate = WorkerLifecycleGate(
        project=project,
        inspect=_inspect,
        register=_register,
        terminate=_terminate,
        credential=lambda: _credential(credential_path),
        inject=lambda container_id, credential: _inject_credential(
            credential_path, container_id, credential
        ),
        start=lambda container_id: _docker_operation("start", container_id),
        stop=lambda container_id: _docker_operation("stop", container_id),
        remove=lambda container_id: _docker_operation("rm", container_id),
    )
    return ComposeWorkerLifecycle(
        command=_command,
        gate=gate,
        prepare_credential=lambda: _prepare_credential_file(credential_path),
        create_environment=lambda: {
            "KDIVE_WORKER_DATABASE_URL": require(WORKER_DATABASE_URL),
        },
        credential_retained=lambda: _credential_retained(credential_path),
        cleanup_credential=lambda: _cleanup_credential(credential_path),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "recreate", "down"))
    parser.add_argument("--project", default=os.environ.get("COMPOSE_PROJECT_NAME", "kdive"))
    parser.add_argument("--volumes", action="store_true")
    return parser


@contextmanager
def _lifecycle_lock(project: str):
    if not _PROJECT.fullmatch(project):
        raise RuntimeError("Compose project name is invalid for the lifecycle lock")
    path = Path(f"/tmp/kdive-compose-worker-{project}.lock")
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another worker lifecycle operation is active for Compose project {project}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    with _lifecycle_lock(args.project):
        lifecycle = _lifecycle(args.project)
        if args.action == "up":
            asyncio.run(lifecycle.up())
        elif args.action == "recreate":
            asyncio.run(lifecycle.recreate())
        else:
            asyncio.run(lifecycle.down(volumes=args.volumes))


if __name__ == "__main__":
    main()
