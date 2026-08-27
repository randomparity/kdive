"""Peer-authenticated one-request control for retained systemd workers."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import socket
import stat
import struct
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from pydantic import ValidationError

from kdive.db.pool import create_pool
from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    LifecycleRequest,
    LifecycleResponse,
    client_exit_status,
)
from kdive.processes.lifecycle.systemd.systemd_worker_lifecycle import (
    PostgresAuthority,
    SystemdWorkerLifecycle,
)
from kdive.processes.lifecycle.systemd.systemd_worker_runtime import (
    Deadline,
    MonotonicDeadline,
    SubprocessCommandRunner,
    SystemdRuntime,
)
from kdive.processes.lifecycle.systemd.systemd_worker_state import SlotStore

_REQUEST_SECONDS = 120.0
_RESPONSE_RESERVE_SECONDS = 1.0
_SOCKET_PATH = Path("/run/kdive/live-worker-lifecycle.sock")
_LOCK_PATH = Path("/run/lock/kdive-live-worker-lifecycle.lock")
_STATE_ROOT = Path("/var/lib/kdive/live-workers")
_PEER_FORMAT = "3i"
_PEER_SIZE = struct.calcsize(_PEER_FORMAT)
_CREDENTIAL_LIMIT = 4096


class PeerRejected(PermissionError):
    """The connected Unix peer is not the one provisioned operator UID."""


class ProtocolRejected(RuntimeError):
    """The lifecycle server returned an unsafe or incomplete response frame."""


class _DeadlineElapsed(TimeoutError):
    """The request exhausted its caller-owned monotonic deadline."""


@dataclass(frozen=True, slots=True)
class PeerCredentials:
    """Linux credentials bound by the kernel to one Unix socket peer."""

    pid: int
    uid: int
    gid: int


class SocketLike(Protocol):
    """Socket operations needed by the one-request protocol."""

    def getsockopt(self, level: int, option: int, size: int, /) -> object: ...

    def settimeout(self, value: float, /) -> None: ...

    def recv(self, size: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...

    def shutdown(self, how: int, /) -> None: ...

    def close(self) -> None: ...


class Lifecycle(Protocol):
    """The operation surface exposed through the control socket."""

    async def start(self, request: LifecycleRequest, deadline: Deadline) -> LifecycleResponse: ...

    async def status(self, deadline: Deadline) -> LifecycleResponse: ...

    async def stop(self, deadline: Deadline) -> LifecycleResponse: ...

    async def diagnostics(self, deadline: Deadline) -> LifecycleResponse: ...


type LifecycleContext = AbstractAsyncContextManager[Lifecycle]
type LifecycleFactory = Callable[[Deadline], object]


@dataclass(frozen=True, slots=True)
class ServiceConfiguration:
    """Privileged inputs loaded only from the root-owned service configuration."""

    expected_uid: int
    state_root: Path
    lock_path: Path
    witness_dsn: str


@dataclass(frozen=True, slots=True)
class _ResponseReservedDeadline:
    """Clip one caller-owned deadline so the parent retains response-write time."""

    parent: Deadline
    reserve_seconds: float

    def remaining(self) -> float:
        return max(0.0, self.parent.remaining() - self.reserve_seconds)


def _peer_credentials(connection: SocketLike) -> PeerCredentials:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEER_SIZE)
    if not isinstance(raw, bytes) or len(raw) != _PEER_SIZE:
        raise PeerRejected("lifecycle peer credentials are incomplete")
    pid, uid, gid = struct.unpack(_PEER_FORMAT, raw)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def _remaining(deadline: Deadline) -> float:
    remaining = deadline.remaining()
    if remaining <= 0:
        raise _DeadlineElapsed("lifecycle request exceeded its monotonic deadline")
    return remaining


def _receive_frame(connection: SocketLike, deadline: Deadline, maximum: int) -> bytes:
    frame = bytearray()
    while True:
        connection.settimeout(_remaining(deadline))
        try:
            chunk = connection.recv(min(8192, maximum + 1 - len(frame)))
        except TimeoutError as exc:
            raise _DeadlineElapsed("lifecycle request framing timed out") from exc
        if not chunk:
            return bytes(frame)
        frame.extend(chunk)
        if len(frame) > maximum:
            raise ValueError("lifecycle request exceeds its byte limit")


def _response(
    code: str,
    message: str,
    retry_action: str,
) -> LifecycleResponse:
    return LifecycleResponse.model_validate(
        {
            "ok": False,
            "code": code,
            "message": message,
            "retry_action": retry_action,
        }
    )


def _send_response(connection: SocketLike, response: LifecycleResponse, deadline: Deadline) -> None:
    try:
        connection.settimeout(_remaining(deadline))
        connection.sendall(response.to_json_bytes())
    except OSError, TimeoutError, _DeadlineElapsed:
        return


def _open_lock(path: Path):
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise PermissionError("lifecycle control lock has untrusted metadata")
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a+b")


async def _call_lifecycle(
    lifecycle: Lifecycle, request: LifecycleRequest, deadline: Deadline
) -> LifecycleResponse:
    if request.operation == "start":
        operation = lifecycle.start(request, deadline)
    else:
        operation = getattr(lifecycle, request.operation)(deadline)
    return await operation


async def _dispatch(
    request: LifecycleRequest, deadline: Deadline, build_lifecycle: LifecycleFactory
) -> LifecycleResponse:
    try:
        built = build_lifecycle(deadline)
        if isinstance(built, AbstractAsyncContextManager):
            async with built as lifecycle:
                return await _call_lifecycle(cast(Lifecycle, lifecycle), request, deadline)
        return await _call_lifecycle(cast(Lifecycle, built), request, deadline)
    except Exception:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError from None
        raise


async def _bounded_dispatch(
    request: LifecycleRequest, deadline: Deadline, build_lifecycle: LifecycleFactory
) -> LifecycleResponse:
    remaining = _remaining(deadline)
    response_reserve = min(_RESPONSE_RESERVE_SECONDS, remaining / 2)
    operation_deadline = _ResponseReservedDeadline(deadline, response_reserve)
    try:
        response = await asyncio.wait_for(
            _dispatch(request, operation_deadline, build_lifecycle),
            timeout=operation_deadline.remaining(),
        )
    except TimeoutError:
        return _response(
            "deadline_exceeded",
            "lifecycle operation exceeded its monotonic deadline",
            "retry_same_operation",
        )
    if operation_deadline.remaining() <= 0:
        return _response(
            "deadline_exceeded",
            "lifecycle operation exceeded its monotonic deadline",
            "retry_same_operation",
        )
    return response


def serve_one(
    connection: SocketLike,
    *,
    expected_uid: int,
    build_lifecycle: LifecycleFactory,
    lock_path: Path = _LOCK_PATH,
    monotonic: Callable[[], float] = time.monotonic,
    request_seconds: float = _REQUEST_SECONDS,
) -> LifecycleResponse:
    """Authenticate, handle, and close exactly one connected Unix socket request."""
    try:
        peer = _peer_credentials(connection)
        if peer.uid != expected_uid:
            raise PeerRejected("lifecycle socket peer does not match the provisioned operator")
        deadline = MonotonicDeadline.after(request_seconds, monotonic=monotonic)
        try:
            frame = _receive_frame(connection, deadline, MAX_REQUEST_BYTES)
            request = LifecycleRequest.model_validate_json(frame)
            _remaining(deadline)
        except _DeadlineElapsed:
            response = _response(
                "deadline_exceeded",
                "lifecycle request framing exceeded its monotonic deadline",
                "retry_same_operation",
            )
            _send_response(connection, response, deadline)
            return response
        except ValueError, ValidationError:
            response = _response(
                "invalid_request", "lifecycle request is malformed", "correct_request"
            )
            _send_response(connection, response, deadline)
            return response
        with _open_lock(lock_path) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                response = _response(
                    "busy", "another lifecycle request is active", "retry_same_operation"
                )
                _send_response(connection, response, deadline)
                return response
            try:
                response = asyncio.run(_bounded_dispatch(request, deadline, build_lifecycle))
            except _DeadlineElapsed:
                response = _response(
                    "deadline_exceeded",
                    "lifecycle operation exceeded its monotonic deadline",
                    "retry_same_operation",
                )
            except Exception:
                response = _response(
                    "internal_error",
                    "lifecycle control assembly failed",
                    "operator_recovery",
                )
            _send_response(connection, response, deadline)
            return response
    finally:
        connection.close()


def request_one(connection: SocketLike, request: LifecycleRequest) -> LifecycleResponse:
    """Send one request, half-close, and require one bounded response followed by EOF."""
    frame = request.to_wire_bytes()
    if len(frame) > MAX_REQUEST_BYTES:
        raise ProtocolRejected("lifecycle request exceeds its byte limit")
    deadline = MonotonicDeadline.after(_REQUEST_SECONDS)
    try:
        connection.settimeout(_remaining(deadline))
        connection.sendall(frame)
        connection.shutdown(socket.SHUT_WR)
        response = _receive_frame(connection, deadline, MAX_RESPONSE_BYTES)
    except (OSError, TimeoutError, ValueError, _DeadlineElapsed) as exc:
        raise ProtocolRejected("lifecycle response did not complete before its deadline") from exc
    finally:
        connection.close()
    try:
        return LifecycleResponse.model_validate_json(response)
    except (ValueError, ValidationError) as exc:
        raise ProtocolRejected("lifecycle response is malformed") from exc


def request_path(path: Path, request: LifecycleRequest) -> LifecycleResponse:
    """Connect to the fixed Unix endpoint and complete one client exchange."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(path))
    except BaseException:
        connection.close()
        raise
    return request_one(connection, request)


def _trusted_regular(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        unsafe_mode = stat.S_IMODE(metadata.st_mode) & 0o077
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_nlink != 1
            or unsafe_mode
            or metadata.st_size > maximum
        ):
            raise PermissionError("lifecycle witness credential has untrusted metadata")
        data = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if not data.strip() or len(data) > maximum:
        raise PermissionError("lifecycle witness credential is empty or oversized")
    return data.strip()


def service_configuration(environment: dict[str, str] | None = None) -> ServiceConfiguration:
    """Load fixed privileged inputs from systemd's root-owned environment and credential mount."""
    env = os.environ if environment is None else environment
    uid = env.get("KDIVE_LIVE_WORKER_OPERATOR_UID", "")
    if not uid.isascii() or not uid.isdecimal():
        raise RuntimeError("service configuration requires a numeric operator UID")
    state_root = Path(env.get("KDIVE_LIVE_WORKER_STATE_ROOT", str(_STATE_ROOT)))
    if not state_root.is_absolute():
        raise RuntimeError("service state root must be absolute")
    credentials = Path(env.get("CREDENTIALS_DIRECTORY", ""))
    if not credentials.is_absolute():
        raise RuntimeError("systemd credentials directory must be absolute")
    dsn = _trusted_regular(credentials / "witness-dsn", _CREDENTIAL_LIMIT).decode("utf-8")
    return ServiceConfiguration(int(uid), state_root, _LOCK_PATH, dsn)


@asynccontextmanager
async def _concrete_lifecycle(
    configuration: ServiceConfiguration, deadline: Deadline
) -> AsyncIterator[Lifecycle]:
    pool = create_pool(configuration.witness_dsn, min_size=1, max_size=1)
    async with pool:
        runner = SubprocessCommandRunner(deadline)
        yield SystemdWorkerLifecycle(
            stores=tuple(
                SlotStore(root=configuration.state_root, slot=slot) for slot in range(1, 9)
            ),
            runtime=SystemdRuntime(runner),
            authority=PostgresAuthority(pool),
        )


def _serve() -> int:
    configuration = service_configuration()
    connection = socket.socket(fileno=0)
    serve_one(
        connection,
        expected_uid=configuration.expected_uid,
        lock_path=configuration.lock_path,
        build_lifecycle=lambda deadline: _concrete_lifecycle(configuration, deadline),
    )
    return 0


def _request(operation: str) -> int:
    if operation == "start":
        try:
            frame = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            if len(frame) > MAX_REQUEST_BYTES:
                raise ValueError("stdin request exceeds the lifecycle request limit")
            request = LifecycleRequest.model_validate_json(frame)
            if request.operation != "start":
                raise ValueError("stdin request operation must be start")
        except ValueError, ValidationError:
            print("invalid lifecycle start request", file=sys.stderr)
            return 2
    else:
        request = LifecycleRequest.model_validate({"operation": operation})
    try:
        response = request_path(_SOCKET_PATH, request)
    except OSError, ProtocolRejected:
        print("lifecycle control response was unavailable or unsafe", file=sys.stderr)
        return 5
    sys.stdout.buffer.write(response.to_json_bytes() + b"\n")
    return client_exit_status(response)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="control retained host worker incarnations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="serve one socket-activated request")
    request = subparsers.add_parser("request", help="send one lifecycle request")
    request.add_argument("operation", choices=("start", "status", "stop", "diagnostics"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the socket-activated server or unprivileged one-request client."""
    arguments = _parser().parse_args(argv)
    if arguments.command == "serve":
        return _serve()
    return _request(cast(str, arguments.operation))


if __name__ == "__main__":
    raise SystemExit(main())
