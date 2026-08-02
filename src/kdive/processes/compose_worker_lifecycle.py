"""Evidence-preserving worker lifecycle for the reference Compose deployment."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import secrets
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

import psycopg

from kdive.config import require
from kdive.config.core_settings import DATABASE_URL
from kdive.processes.docker_death_api import WorkerLifecycleGate
from kdive.services.runs.worker_incarnations import (
    register_worker_incarnation,
    terminate_worker_incarnation,
)

type Command = Callable[[tuple[str, ...], dict[str, str] | None], str]

_COMPOSE = ("docker", "compose")
_PROFILE = ("--profile", "managed-worker")
_PROJECT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}")


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
    ) -> None:
        self._command = command
        self._gate = gate
        self._nonce = nonce

    def _worker_id(self) -> str | None:
        value = self._command((*_COMPOSE, *_PROFILE, "ps", "--all", "-q", "worker"), None).strip()
        return value or None

    async def _create(self) -> None:
        nonce = self._nonce()
        self._command(
            (*_COMPOSE, *_PROFILE, "create", "--no-recreate", "worker"),
            {"KDIVE_WORKER_INCARNATION_NONCE": nonce},
        )
        container_id = self._worker_id()
        if container_id is None:
            raise RuntimeError("Compose did not retain the created worker container")
        await self._gate.register_and_start(container_id)

    async def up(self) -> None:
        """Start the non-worker graph, then create, bind, and start the worker."""
        self._command((*_COMPOSE, "up", "-d", "--wait", "--wait-timeout", "120"), None)
        current = self._worker_id()
        if current is None or await self._gate.reconcile(current):
            await self._create()

    async def recreate(self) -> None:
        """Terminate the old generation before creating its replacement."""
        current = self._worker_id()
        if current is not None:
            await self._gate.terminate_and_remove(current)
        await self._create()

    async def down(self, *, volumes: bool = False) -> None:
        """Record worker termination before removing the database and gate."""
        current = self._worker_id()
        if current is not None:
            await self._gate.terminate_and_remove(current)
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


async def _register(holder: str, container_id: str) -> None:
    conn = await psycopg.AsyncConnection.connect(require(DATABASE_URL))
    try:
        await register_worker_incarnation(conn, holder, "docker", {"container_id": container_id})
    finally:
        await conn.close()


async def _terminate(holder: str, outcome: str) -> None:
    if outcome not in {"succeeded", "failed", "killed"}:
        raise RuntimeError("Docker lifecycle produced an unsupported termination outcome")
    conn = await psycopg.AsyncConnection.connect(require(DATABASE_URL))
    try:
        await terminate_worker_incarnation(conn, holder, outcome)  # type: ignore[arg-type]
    finally:
        await conn.close()


def _lifecycle(project: str) -> ComposeWorkerLifecycle:
    gate = WorkerLifecycleGate(
        project=project,
        inspect=_inspect,
        register=_register,
        terminate=_terminate,
        start=lambda container_id: _docker_operation("start", container_id),
        stop=lambda container_id: _docker_operation("stop", container_id),
        remove=lambda container_id: _docker_operation("rm", container_id),
    )
    return ComposeWorkerLifecycle(command=_command, gate=gate)


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
