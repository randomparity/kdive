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
import selectors
import stat
import subprocess
import tarfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Protocol

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
type CleanupManagedVolumes = Callable[[], None]

_COMPOSE = ("docker", "compose")
_PROFILE = ("--profile", "managed-worker")
_PROJECT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}")
_FULL_ID = re.compile(r"[0-9a-f]{64}")
_CREDENTIAL = re.compile(r"[0-9a-f]{64}")
_INSPECT_TIMEOUT_SECONDS = 5
_INSPECT_STDOUT_BYTES = 1_048_576
_INSPECT_STDERR_BYTES = 65_536
_COMMAND_STDOUT_BYTES = 1_048_576
_COMMAND_STDERR_BYTES = 1_048_576
_COMMAND_TIMEOUTS = {
    "compose-up": 600,
    "compose-create": 120,
    "compose-ps": 30,
    "compose-down": 120,
    "start": 30,
    "stop": 45,
    "rm": 30,
}


class CommandOutputTooLarge(RuntimeError):
    """A child command exceeded one of its bounded output streams."""

    def __init__(self, stream: str, limit: int) -> None:
        super().__init__(f"{stream} exceeded its {limit}-byte bound")
        self.stream = stream
        self.limit = limit


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
        cleanup_managed_volumes: CleanupManagedVolumes = lambda: None,
    ) -> None:
        self._command = command
        self._gate = gate
        self._nonce = nonce
        self._prepare_credential = prepare_credential
        self._create_environment = create_environment
        self._credential_retained = credential_retained
        self._cleanup_credential = cleanup_credential
        self._cleanup_managed_volumes = cleanup_managed_volumes

    async def _run_command(
        self, argv: tuple[str, ...], extra_env: dict[str, str] | None = None
    ) -> str:
        return await asyncio.to_thread(self._command, argv, extra_env)

    async def _worker_id(self) -> str | None:
        value = (
            await self._run_command((*_COMPOSE, *_PROFILE, "ps", "--all", "-q", "worker"))
        ).strip()
        return value or None

    async def _require_safe_absence(self) -> None:
        if await asyncio.to_thread(self._credential_retained):
            raise RuntimeError(
                "managed worker is absent but its retained worker credential remains"
            )

    async def _create(self) -> None:
        await asyncio.to_thread(self._prepare_credential)
        nonce = self._nonce()
        await self._run_command(
            (*_COMPOSE, *_PROFILE, "create", "--no-recreate", "worker"),
            {
                "KDIVE_WORKER_INCARNATION_NONCE": nonce,
                **self._create_environment(),
            },
        )
        container_id = await self._worker_id()
        if container_id is None:
            raise RuntimeError("Compose did not retain the created worker container")
        await self._gate.register_and_start(container_id)

    async def up(self) -> None:
        """Start the non-worker graph, then create, bind, and start the worker."""
        await self._run_command((*_COMPOSE, "up", "-d", "--wait", "--wait-timeout", "120"))
        current = await self._worker_id()
        if current is None:
            await self._require_safe_absence()
            await self._create()
        elif await self._gate.reconcile(current):
            await asyncio.to_thread(self._cleanup_credential)
            await self._create()

    async def recreate(self) -> None:
        """Terminate the old generation before creating its replacement."""
        current = await self._worker_id()
        if current is not None:
            await self._gate.terminate_and_remove(current)
            await asyncio.to_thread(self._cleanup_credential)
        else:
            await self._require_safe_absence()
        await self._create()

    async def down(self, *, volumes: bool = False) -> None:
        """Record worker termination before removing the database and gate."""
        current = await self._worker_id()
        if current is not None:
            await self._gate.terminate_and_remove(current)
            await asyncio.to_thread(self._cleanup_credential)
        else:
            await self._require_safe_absence()
        args = (*_COMPOSE, "down", *(("--volumes",) if volumes else ()), "--remove-orphans")
        await self._run_command(args)
        if volumes:
            await asyncio.to_thread(self._cleanup_managed_volumes)


def _bounded_command(
    argv: tuple[str, ...],
    extra_env: dict[str, str] | None,
    *,
    timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> str:
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **(extra_env or {})},
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen contract
        process.kill()
        raise RuntimeError("bounded lifecycle command did not expose output pipes")
    streams: dict[int, tuple[str, int, IO[bytes]]] = {
        process.stdout.fileno(): ("stdout", max_stdout_bytes, process.stdout),
        process.stderr.fileno(): ("stderr", max_stderr_bytes, process.stderr),
    }
    output = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    for descriptor in streams:
        selector.register(descriptor, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            ready = selector.select(remaining)
            if not ready:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in ready:
                descriptor = key.fd
                name, limit, stream = streams[descriptor]
                chunk = os.read(descriptor, min(65_536, limit - len(output[name]) + 1))
                if not chunk:
                    selector.unregister(descriptor)
                    stream.close()
                    continue
                output[name].extend(chunk)
                if len(output[name]) > limit:
                    raise CommandOutputTooLarge(name, limit)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        returncode = process.wait(timeout=remaining)
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        for _, _, stream in streams.values():
            if not stream.closed:
                stream.close()
    stdout = output["stdout"].decode("utf-8", errors="replace")
    stderr = output["stderr"].decode("utf-8", errors="replace")
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, argv, output=stdout, stderr=stderr)
    return stdout


def _command_timeout(argv: tuple[str, ...]) -> int:
    if len(argv) >= 3 and argv[:2] == _COMPOSE:
        for operation in ("up", "create", "ps", "down"):
            if operation in argv[2:]:
                return _COMMAND_TIMEOUTS[f"compose-{operation}"]
    if len(argv) >= 2 and argv[0] == "docker" and argv[1] in {"start", "stop", "rm"}:
        return _COMMAND_TIMEOUTS[argv[1]]
    return 120


def _command(argv: tuple[str, ...], extra_env: dict[str, str] | None = None) -> str:
    return _bounded_command(
        argv,
        extra_env,
        timeout=_command_timeout(argv),
        max_stdout_bytes=_COMMAND_STDOUT_BYTES,
        max_stderr_bytes=_COMMAND_STDERR_BYTES,
    )


def _project_command(
    project: str, argv: tuple[str, ...], extra_env: dict[str, str] | None = None
) -> str:
    return _command(argv, {"COMPOSE_PROJECT_NAME": project, **(extra_env or {})})


def _inspect(container_id: str) -> Mapping[str, object] | None:
    if not _FULL_ID.fullmatch(container_id):
        raise RuntimeError("Docker inspect requires an exact 64-character container ID")
    try:
        result = _bounded_command(
            ("docker", "inspect", container_id),
            None,
            timeout=_INSPECT_TIMEOUT_SECONDS,
            max_stdout_bytes=_INSPECT_STDOUT_BYTES,
            max_stderr_bytes=_INSPECT_STDERR_BYTES,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Docker inspect exceeded its 5-second bound") from exc
    except CommandOutputTooLarge as exc:
        raise RuntimeError("Docker inspect output exceeded its bounded transport") from exc
    try:
        decoded = json.loads(result)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError("Docker inspect returned invalid bounded JSON") from exc
    return _bounded_inspect_schema(decoded)


def _bounded_inspect_schema(decoded: object) -> Mapping[str, object] | None:
    if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
        return None
    container = decoded[0]
    container_id = container.get("Id")
    config = container.get("Config")
    state = container.get("State")
    if (
        not isinstance(container_id, str)
        or not _FULL_ID.fullmatch(container_id)
        or not isinstance(config, dict)
        or not isinstance(state, dict)
    ):
        raise RuntimeError("Docker inspect identity does not match the bounded schema")
    labels = config.get("Labels")
    environment = config.get("Env")
    status = state.get("Status")
    exit_code = state.get("ExitCode")
    if (
        not isinstance(labels, dict)
        or len(labels) > 256
        or any(
            not isinstance(key, str)
            or len(key) > 1_024
            or not isinstance(value, str)
            or len(value) > 1_024
            for key, value in labels.items()
        )
        or not isinstance(environment, list)
        or len(environment) > 512
        or any(not isinstance(value, str) or len(value) > 4_096 for value in environment)
        or not isinstance(status, str)
        or len(status) > 32
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
    ):
        raise RuntimeError("Docker inspect identity does not match the bounded schema")
    return {
        "Id": container_id,
        "Config": {"Labels": labels, "Env": environment},
        "State": {"Status": status, "ExitCode": exit_code},
    }


async def _docker_operation(operation: str, container_id: str) -> None:
    await asyncio.to_thread(_command, ("docker", operation, container_id), None)


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


async def _terminate(holder: str, binding: dict[str, str], outcome: str) -> None:
    if outcome not in {"succeeded", "failed", "killed"}:
        raise RuntimeError("Docker lifecycle produced an unsupported termination outcome")
    conn = await psycopg.AsyncConnection.connect(require(LIFECYCLE_WITNESS_DATABASE_URL))
    try:
        await terminate_worker_incarnation(
            conn,
            holder,
            "docker",
            binding,
            outcome,  # type: ignore[arg-type]
        )
    finally:
        await conn.close()


def _credential_path(project: str) -> Path:
    return Path(f"/tmp/kdive-compose-worker-{project}.credential")


def _remove_managed_worker_volumes(project: str) -> None:
    """Remove the two exact profile-only volumes omitted after worker removal."""
    if not _PROJECT.fullmatch(project):
        raise RuntimeError("Compose project name is invalid for managed-volume cleanup")
    _bounded_command(
        (
            "docker",
            "volume",
            "rm",
            "--force",
            f"{project}_kdive-build",
            f"{project}_kdive-install",
        ),
        None,
        timeout=30,
        max_stdout_bytes=65_536,
        max_stderr_bytes=65_536,
    )


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
    await asyncio.to_thread(_inject_credential_blocking, path, container_id, credential)


def _inject_credential_blocking(path: Path, container_id: str, credential: str) -> None:
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
        command=lambda argv, env: _project_command(project, argv, env),
        gate=gate,
        prepare_credential=lambda: _prepare_credential_file(credential_path),
        create_environment=lambda: {
            "KDIVE_WORKER_DATABASE_URL": require(WORKER_DATABASE_URL),
        },
        credential_retained=lambda: _credential_retained(credential_path),
        cleanup_credential=lambda: _cleanup_credential(credential_path),
        cleanup_managed_volumes=lambda: _remove_managed_worker_volumes(project),
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
