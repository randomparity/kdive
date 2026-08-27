"""Peer-authenticated, bounded host worker lifecycle control protocol."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import kdive.processes.lifecycle.systemd.systemd_worker_control as worker_control
from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    LifecycleRequest,
    LifecycleResponse,
)
from kdive.processes.lifecycle.systemd.systemd_worker_control import (
    PeerCredentials,
    PeerRejected,
    ProtocolRejected,
    request_one,
    serve_one,
    service_configuration,
)
from kdive.processes.lifecycle.systemd.systemd_worker_runtime import Deadline
from tests.processes.lifecycle.systemd.systemd_worker_support import start_payload


class FakeClock:
    """Controllable monotonic source."""

    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value


class FakeSocket:
    """Small socket double that exposes framing order and deadline use."""

    def __init__(self, *, uid: int, chunks: list[bytes]) -> None:
        self.peer = PeerCredentials(pid=10, uid=uid, gid=uid)
        self.chunks = chunks
        self.recv_calls = 0
        self.sent = bytearray()
        self.timeouts: list[float] = []
        self.closed = False
        self.on_recv: Callable[[], None] | None = None

    def getsockopt(self, _level: int, _option: int, _size: int) -> bytes:
        return struct.pack("3i", self.peer.pid, self.peer.uid, self.peer.gid)

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def recv(self, _size: int) -> bytes:
        self.recv_calls += 1
        if self.on_recv is not None:
            self.on_recv()
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def shutdown(self, _how: int) -> None:
        return

    def close(self) -> None:
        self.closed = True


class FakeLifecycle:
    """Record dispatch without touching host lifecycle state."""

    def __init__(
        self, *, entered: threading.Event | None = None, release: threading.Event | None = None
    ):
        self.operations: list[str] = []
        self.requests: list[LifecycleRequest] = []
        self.deadlines: list[Deadline] = []
        self.entered = entered
        self.release = release

    async def start(self, request: LifecycleRequest, deadline: Deadline) -> LifecycleResponse:
        self.operations.append("start")
        self.requests.append(request)
        self.deadlines.append(deadline)
        return _response()

    async def status(self, deadline: Deadline) -> LifecycleResponse:
        self.operations.append("status")
        self.deadlines.append(deadline)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=5)
        return _response()

    async def stop(self, deadline: Deadline) -> LifecycleResponse:
        self.operations.append("stop")
        self.deadlines.append(deadline)
        return _response()

    async def diagnostics(self, deadline: Deadline) -> LifecycleResponse:
        self.operations.append("diagnostics")
        self.deadlines.append(deadline)
        return _response()


def _response() -> LifecycleResponse:
    return LifecycleResponse(
        ok=True,
        code="ok",
        message="worker fleet status",
        retry_action="none",
    )


def _status_frame() -> bytes:
    return LifecycleRequest(operation="status").model_dump_json().encode("utf-8")


def _decoded(fake_socket: FakeSocket) -> dict[str, object]:
    return json.loads(fake_socket.sent)


def test_server_checks_peer_before_request_read(tmp_path: Path) -> None:
    fake_socket = FakeSocket(uid=2000, chunks=[_status_frame(), b""])

    with pytest.raises(PeerRejected):
        serve_one(
            fake_socket,
            expected_uid=1000,
            lock_path=tmp_path / "control.lock",
            build_lifecycle=lambda _deadline: pytest.fail("foreign peer reached assembly"),
        )

    assert fake_socket.recv_calls == 0
    assert fake_socket.closed


def test_server_rejects_request_above_32_kib(tmp_path: Path) -> None:
    valid = b'{"operation":"status"}'
    frame = valid + b" " * (MAX_REQUEST_BYTES + 1 - len(valid))
    assert len(frame) == MAX_REQUEST_BYTES + 1
    fake_socket = FakeSocket(uid=1000, chunks=[frame])
    lock_path = tmp_path / "control.lock"

    response = serve_one(
        fake_socket,
        expected_uid=1000,
        lock_path=lock_path,
        build_lifecycle=lambda _deadline: pytest.fail("oversized request reached assembly"),
    )

    assert response.code == "invalid_request"
    assert response.retry_action == "correct_request"
    assert _decoded(fake_socket)["code"] == "invalid_request"
    assert not lock_path.exists()


def test_held_nonblocking_lock_returns_busy(tmp_path: Path) -> None:
    lock_path = tmp_path / "control.lock"
    with lock_path.open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fake_socket = FakeSocket(uid=1000, chunks=[_status_frame(), b""])

        response = serve_one(
            fake_socket,
            expected_uid=1000,
            lock_path=lock_path,
            build_lifecycle=lambda _deadline: pytest.fail("busy request reached assembly"),
        )

    assert (response.code, response.retry_action) == ("busy", "retry_same_operation")


def test_deadline_starts_after_peer_auth_and_is_shared_with_lifecycle(tmp_path: Path) -> None:
    clock = FakeClock()
    fake_socket = FakeSocket(uid=1000, chunks=[_status_frame(), b""])
    lifecycle = FakeLifecycle()

    response = serve_one(
        fake_socket,
        expected_uid=1000,
        lock_path=tmp_path / "control.lock",
        build_lifecycle=lambda deadline: lifecycle,
        monotonic=clock,
    )

    assert response.ok
    assert lifecycle.operations == ["status"]
    assert len(lifecycle.deadlines) == 1
    assert lifecycle.deadlines[0].remaining() == 119.0
    assert fake_socket.timeouts[0] == 120.0


@pytest.mark.parametrize(
    "lifecycle_request",
    [
        LifecycleRequest.model_validate(start_payload()),
        LifecycleRequest(operation="status"),
        LifecycleRequest(operation="stop"),
        LifecycleRequest(operation="diagnostics"),
    ],
)
def test_server_dispatches_each_validated_operation_exactly_once(
    tmp_path: Path, lifecycle_request: LifecycleRequest
) -> None:
    fake_socket = FakeSocket(
        uid=1000,
        chunks=[lifecycle_request.model_dump_json().encode("utf-8"), b""],
    )
    lifecycle = FakeLifecycle()

    response = serve_one(
        fake_socket,
        expected_uid=1000,
        lock_path=tmp_path / "control.lock",
        build_lifecycle=lambda _deadline: lifecycle,
    )

    assert response.ok
    assert lifecycle.operations == [lifecycle_request.operation]
    if lifecycle_request.operation == "start":
        assert len(lifecycle.requests) == 1
        assert lifecycle.requests[0].model_dump_json() == lifecycle_request.model_dump_json()
    else:
        assert lifecycle.requests == []
        assert lifecycle_request.worker_count is None and lifecycle_request.settings is None


def test_assembly_failure_returns_fixed_response_without_external_text(tmp_path: Path) -> None:
    secret = "witness-dsn-secret-must-not-escape"  # pragma: allowlist secret
    fake_socket = FakeSocket(uid=1000, chunks=[_status_frame(), b""])

    def fail_assembly(_deadline: Deadline) -> object:
        raise RuntimeError(secret)

    response = serve_one(
        fake_socket,
        expected_uid=1000,
        lock_path=tmp_path / "control.lock",
        build_lifecycle=fail_assembly,
    )

    assert (response.code, response.retry_action) == ("internal_error", "operator_recovery")
    assert secret.encode() not in fake_socket.sent


class BlockingLifecycleContext:
    """Block one async resource edge until the request timeout cancels it."""

    def __init__(self, lifecycle: FakeLifecycle, *, block: str) -> None:
        self.lifecycle = lifecycle
        self.block = block
        self.cancel_secret = "async-context-cancel-secret"  # pragma: allowlist secret
        self.entry_cancelled = threading.Event()
        self.exit_cancelled = threading.Event()

    async def __aenter__(self) -> FakeLifecycle:
        if self.block == "entry":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.entry_cancelled.set()
                raise RuntimeError(self.cancel_secret) from None
        return self.lifecycle

    async def __aexit__(self, *_exc: object) -> None:
        if self.block == "exit":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.exit_cancelled.set()
                raise RuntimeError(self.cancel_secret) from None


class SynchronousLateLifecycle(FakeLifecycle):
    """Return success only after synchronously exhausting the supplied child deadline."""

    async def status(self, deadline: Deadline) -> LifecycleResponse:
        self.operations.append("status")
        self.deadlines.append(deadline)
        time.sleep(deadline.remaining() + 0.01)
        return _response()


def _assert_lock_released(lock_path: Path) -> None:
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_deadline_wraps_blocked_async_context_entry(tmp_path: Path) -> None:
    lock_path = tmp_path / "control.lock"
    fake_socket = FakeSocket(uid=1000, chunks=[_status_frame(), b""])
    lifecycle = FakeLifecycle()
    context = BlockingLifecycleContext(lifecycle, block="entry")

    response = serve_one(
        fake_socket,
        expected_uid=1000,
        lock_path=lock_path,
        build_lifecycle=lambda _deadline: context,
        request_seconds=0.05,
    )

    assert (response.code, response.retry_action) == (
        "deadline_exceeded",
        "retry_same_operation",
    )
    assert context.entry_cancelled.is_set()
    assert lifecycle.operations == []
    assert b"deadline_exceeded" in fake_socket.sent
    assert context.cancel_secret.encode() not in fake_socket.sent
    _assert_lock_released(lock_path)


def test_deadline_wraps_blocked_async_context_exit_and_cancels_cleanup(tmp_path: Path) -> None:
    lock_path = tmp_path / "control.lock"
    fake_socket = FakeSocket(uid=1000, chunks=[_status_frame(), b""])
    lifecycle = FakeLifecycle()
    context = BlockingLifecycleContext(lifecycle, block="exit")
    factory_deadlines: list[Deadline] = []

    def build_lifecycle(deadline: Deadline) -> BlockingLifecycleContext:
        factory_deadlines.append(deadline)
        return context

    response = serve_one(
        fake_socket,
        expected_uid=1000,
        lock_path=lock_path,
        build_lifecycle=build_lifecycle,
        request_seconds=0.05,
    )

    assert (response.code, response.retry_action) == (
        "deadline_exceeded",
        "retry_same_operation",
    )
    assert lifecycle.operations == ["status"]
    assert lifecycle.deadlines == factory_deadlines
    assert context.exit_cancelled.is_set()
    assert b"deadline_exceeded" in fake_socket.sent
    assert context.cancel_secret.encode() not in fake_socket.sent
    _assert_lock_released(lock_path)


def test_child_deadline_rejects_late_synchronous_success_before_parent_write(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "control.lock"
    fake_socket = FakeSocket(uid=1000, chunks=[_status_frame(), b""])
    lifecycle = SynchronousLateLifecycle()
    started = time.monotonic()

    response = serve_one(
        fake_socket,
        expected_uid=1000,
        lock_path=lock_path,
        build_lifecycle=lambda _deadline: lifecycle,
        request_seconds=0.2,
    )

    assert time.monotonic() - started < 0.2
    assert (response.code, response.retry_action) == (
        "deadline_exceeded",
        "retry_same_operation",
    )
    assert lifecycle.operations == ["status"]
    assert b"deadline_exceeded" in fake_socket.sent
    _assert_lock_released(lock_path)


def test_client_that_never_half_closes_times_out_without_dispatch(tmp_path: Path) -> None:
    clock = FakeClock()
    fake_socket = FakeSocket(uid=1000, chunks=[])
    lifecycle = FakeLifecycle()

    def elapse() -> None:
        clock.value = 131.0
        raise TimeoutError

    fake_socket.on_recv = elapse
    response = serve_one(
        fake_socket,
        expected_uid=1000,
        lock_path=tmp_path / "control.lock",
        build_lifecycle=lambda _deadline: lifecycle,
        monotonic=clock,
    )

    assert response.code == "deadline_exceeded"
    assert lifecycle.operations == []
    assert fake_socket.closed


def test_request_expired_during_framing_never_reaches_lock_or_dispatch(tmp_path: Path) -> None:
    clock = FakeClock()
    lock_path = tmp_path / "control.lock"
    fake_socket = FakeSocket(uid=1000, chunks=[_status_frame(), b""])
    lifecycle = FakeLifecycle()
    fake_socket.on_recv = lambda: setattr(clock, "value", 131.0)

    response = serve_one(
        fake_socket,
        expected_uid=1000,
        lock_path=lock_path,
        build_lifecycle=lambda _deadline: lifecycle,
        monotonic=clock,
    )

    assert response.code == "deadline_exceeded"
    assert lifecycle.operations == []
    assert not lock_path.exists()


def test_real_socketpair_completes_send_half_close_read_eof(tmp_path: Path) -> None:
    server, client = socket.socketpair()
    lifecycle = FakeLifecycle()
    result: list[LifecycleResponse] = []
    thread = threading.Thread(
        target=lambda: result.append(
            serve_one(
                server,
                expected_uid=os.getuid(),
                lock_path=tmp_path / "control.lock",
                build_lifecycle=lambda _deadline: lifecycle,
            )
        )
    )
    thread.start()

    response = request_one(client, LifecycleRequest(operation="status"))

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert response == result[0] == _response()
    assert lifecycle.operations == ["status"]


def test_real_socketpair_preserves_start_secrets_for_authenticated_server(tmp_path: Path) -> None:
    server, client = socket.socketpair()
    lifecycle = FakeLifecycle()
    thread = threading.Thread(
        target=lambda: serve_one(
            server,
            expected_uid=os.getuid(),
            lock_path=tmp_path / "control.lock",
            build_lifecycle=lambda _deadline: lifecycle,
        )
    )
    thread.start()

    response = request_one(client, LifecycleRequest.model_validate(start_payload()))

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert response.ok
    assert len(lifecycle.requests) == 1
    settings = lifecycle.requests[0].settings
    assert settings is not None
    assert (
        settings.worker_database_url.get_secret_value()
        == "postgresql://worker:password@db/kdive"  # pragma: allowlist secret
    )
    assert settings.aws_access_key_id.get_secret_value() == "access-key"
    assert (
        settings.aws_secret_access_key.get_secret_value()
        == "secret-key"  # pragma: allowlist secret
    )


def test_second_socket_instance_reaches_host_lock_and_returns_busy(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    first_lifecycle = FakeLifecycle(entered=entered, release=release)
    first_server, first_client = socket.socketpair()
    first_thread = threading.Thread(
        target=lambda: serve_one(
            first_server,
            expected_uid=os.getuid(),
            lock_path=tmp_path / "control.lock",
            build_lifecycle=lambda _deadline: first_lifecycle,
        )
    )
    first_thread.start()
    first_client.sendall(_status_frame())
    first_client.shutdown(socket.SHUT_WR)
    assert entered.wait(timeout=5)

    second_server, second_client = socket.socketpair()
    second_thread = threading.Thread(
        target=lambda: serve_one(
            second_server,
            expected_uid=os.getuid(),
            lock_path=tmp_path / "control.lock",
            build_lifecycle=lambda _deadline: pytest.fail("busy request reached assembly"),
        )
    )
    second_thread.start()
    response = request_one(second_client, LifecycleRequest(operation="status"))

    release.set()
    first_client.recv(MAX_RESPONSE_BYTES + 1)
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    assert (response.code, response.retry_action) == ("busy", "retry_same_operation")


def test_client_rejects_response_above_protocol_limit() -> None:
    server, client = socket.socketpair()

    def send_oversized() -> None:
        with server:
            server.recv(4096)
            server.sendall(b"x" * (MAX_RESPONSE_BYTES + 1))

    thread = threading.Thread(target=send_oversized)
    thread.start()
    with pytest.raises(ProtocolRejected, match="response"):
        request_one(client, LifecycleRequest(operation="status"))
    thread.join(timeout=5)


@pytest.mark.parametrize(
    "body,secret",
    [
        (b'{"operation":"status"}' + b" " * MAX_REQUEST_BYTES, b""),
        (
            # pragma: allowlist nextline secret
            b'{"operation":"start","caller_secret":"caller-secret-must-not-be-echoed"}',
            b"caller-secret-must-not-be-echoed",
        ),
    ],
)
def test_start_cli_rejects_unsafe_stdin_without_echo_or_traceback(
    body: bytes, secret: bytes
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kdive.processes.lifecycle.systemd.systemd_worker_control",
            "request",
            "start",
        ],
        input=body,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert b"invalid lifecycle start request" in result.stderr
    assert not secret or secret not in result.stderr
    assert b"Traceback" not in result.stderr


def test_service_configuration_requires_root_only_witness_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    witness = credentials / "witness-dsn"
    witness.write_text("postgresql://witness/db\n", encoding="utf-8")
    witness.chmod(0o644)
    real_fstat = os.fstat

    def root_owned_fstat(descriptor: int):
        metadata = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_uid=0,
            st_nlink=metadata.st_nlink,
            st_size=metadata.st_size,
        )

    monkeypatch.setattr(worker_control.os, "fstat", root_owned_fstat)
    environment = {
        "CREDENTIALS_DIRECTORY": str(credentials),
        "KDIVE_LIVE_WORKER_OPERATOR_UID": "1000",
    }

    with pytest.raises(PermissionError, match="untrusted metadata"):
        service_configuration(environment)
