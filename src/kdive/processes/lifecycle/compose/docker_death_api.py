"""Inspect-only Docker API proxy for authoritative Compose worker-death evidence."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from kdive.services.runs.worker_incarnations import DockerAuthorityBinding
from kdive.worker_lifecycle.contracts import TerminationOutcome

_INSPECT_PATH = re.compile(r"/containers/[0-9a-f]{64}/json")
_MAX_RESPONSE_BYTES = 1_048_576
_DOCKER_SOCKET = "/var/run/docker.sock"
_FULL_ID = re.compile(r"[0-9a-f]{64}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_CREDENTIAL = re.compile(r"[0-9a-f]{64}")

type Inspect = Callable[[str], Mapping[str, object] | None]
type Register = Callable[[str, str, bytes], Awaitable[None]]
type Terminate = Callable[[str, DockerAuthorityBinding, TerminationOutcome], Awaitable[None]]
type ContainerOperation = Callable[[str], Awaitable[None]]
type Credential = Callable[[], str]
type InjectCredential = Callable[[str, str], Awaitable[None]]


def _nested_mapping(value: object) -> Mapping[str, object] | None:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else None


@dataclass(frozen=True, slots=True)
class WorkerLifecycleGate:
    """Order exact Docker lifecycle operations around durable incarnation state."""

    project: str
    inspect: Inspect
    register: Register
    terminate: Terminate
    credential: Credential
    inject: InjectCredential
    start: ContainerOperation
    stop: ContainerOperation
    remove: ContainerOperation

    def _identity(self, container_id: str) -> tuple[str, Mapping[str, object]]:
        if not _FULL_ID.fullmatch(container_id):
            raise RuntimeError("worker lifecycle requires an exact 64-character container ID")
        container = self.inspect(container_id)
        if container is None or container.get("Id") != container_id:
            raise RuntimeError("Docker did not return the exact worker container")
        config = _nested_mapping(container.get("Config"))
        labels = _nested_mapping(config.get("Labels") if config else None)
        environment = config.get("Env") if config else None
        expected = {
            "com.docker.compose.project": self.project,
            "com.docker.compose.service": "worker",
            "io.kdive.managed-worker": "true",
        }
        if labels is None or any(labels.get(key) != value for key, value in expected.items()):
            raise RuntimeError("container is not this Compose project's managed worker")
        if not isinstance(environment, list):
            raise RuntimeError("managed worker has no bounded incarnation environment")
        prefix = "KDIVE_WORKER_INCARNATION_ID=docker:"
        values = [
            value.removeprefix(prefix)
            for value in environment
            if isinstance(value, str) and value.startswith(prefix)
        ]
        if len(values) != 1 or not _NONCE.fullmatch(values[0]):
            raise RuntimeError("managed worker has no exact injected incarnation nonce")
        return f"docker:{values[0]}", container

    async def register_and_start(self, container_id: str) -> None:
        """Persist the nonce/full-ID binding before the never-started worker starts."""
        holder, container = await asyncio.to_thread(self._identity, container_id)
        state = _nested_mapping(container.get("State"))
        if state is None or state.get("Status") != "created":
            raise RuntimeError("only a never-started worker may be registered")
        credential = await asyncio.to_thread(self.credential)
        if not _CREDENTIAL.fullmatch(credential):
            raise RuntimeError("worker lifecycle credential must be a 256-bit lowercase hex value")
        credential_hash = hashlib.sha256(credential.encode()).digest()
        await self.register(holder, container_id, credential_hash)
        await self.inject(container_id, credential)
        await self.start(container_id)

    async def reconcile(self, container_id: str) -> bool:
        """Reconcile one retained worker; return whether a replacement must be created."""
        holder, container = await asyncio.to_thread(self._identity, container_id)
        state = _nested_mapping(container.get("State"))
        status = state.get("Status") if state is not None else None
        if status == "created":
            await self.register_and_start(container_id)
            return False
        if status == "running":
            # Exact idempotent registration proves the running container matches its active row.
            credential = await asyncio.to_thread(self.credential)
            if not _CREDENTIAL.fullmatch(credential):
                raise RuntimeError(
                    "worker lifecycle credential must be a 256-bit lowercase hex value"
                )
            await self.register(holder, container_id, hashlib.sha256(credential.encode()).digest())
            return False
        if status in {"exited", "dead"}:
            await self.terminate_and_remove(container_id)
            return True
        raise RuntimeError("managed worker has no reconcilable retained Docker state")

    async def terminate_and_remove(self, container_id: str) -> None:
        """Persist exact terminal evidence before removing Docker's retained record."""
        holder, container = await asyncio.to_thread(self._identity, container_id)
        state = _nested_mapping(container.get("State"))
        if state is None:
            raise RuntimeError("managed worker has no authoritative Docker state")
        if state.get("Status") not in {"exited", "dead"}:
            await self.stop(container_id)
            holder, container = await asyncio.to_thread(self._identity, container_id)
            state = _nested_mapping(container.get("State"))
        if state is None or state.get("Status") not in {"exited", "dead"}:
            raise RuntimeError("exact worker container did not reach a retained terminal state")
        exit_code = state.get("ExitCode")
        outcome = "succeeded" if exit_code == 0 else "killed" if exit_code == 137 else "failed"
        await self.terminate(holder, {"container_id": container_id}, outcome)
        await self.remove(container_id)


def permitted_inspect_path(method: str, path: str) -> bool:
    """Return whether a request is the sole Docker operation this authority permits."""
    return method == "GET" and _INSPECT_PATH.fullmatch(path) is not None


class _UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(_DOCKER_SOCKET)


class _Handler(BaseHTTPRequestHandler):
    server_version = "kdive-worker-death-api"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler contract
        if not permitted_inspect_path("GET", self.path):
            self.send_error(403)
            return
        connection = _UnixHTTPConnection("localhost", timeout=3)
        try:
            connection.request("GET", self.path)
            response = connection.getresponse()
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        except OSError, http.client.HTTPException:
            self.send_error(502)
            return
        finally:
            connection.close()
        if len(body) > _MAX_RESPONSE_BYTES:
            self.send_error(502)
            return
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler contract
        self.send_error(403)

    def do_DELETE(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler contract
        self.send_error(403)

    def do_PUT(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler contract
        self.send_error(403)

    def log_message(self, format: str, *args: object) -> None:
        # Do not log caller-controlled paths from this internal authority.
        return


def main() -> None:
    """Serve the private Compose authority until the container is stopped."""
    server = ThreadingHTTPServer(("0.0.0.0", 2375), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
