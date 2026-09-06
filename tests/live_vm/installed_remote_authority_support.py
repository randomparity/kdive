"""Closed installed remote-authority proof inputs and fixed SSH controls."""

from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import signal
import stat
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import IO, Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_ENV = "KDIVE_LIVE_VM_REMOTE_AUTHORITY_CONFIG"
_PREFIX = re.compile(r"kdive-2151-[0-9a-f]{12}-[0-9a-f]{8}")
_PROOF_SOCKET = Path("/run/kdive/provider-authority/proof-control/control.sock")
_AUTHORITY_PYTHON = "/usr/bin/python3"
_MAX_CONFIG_BYTES = 16_384
_MAX_CONTROL_BYTES = 1024

_FAULT_BARRIER_METADATA = """
import os
import pwd
import stat

path = "/run/kdive/provider-authority/proof-control/control.sock"
metadata = os.stat(path, follow_symlinks=False)
if (not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != pwd.getpwnam("kdive-provider-authority").pw_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600):
    raise SystemExit("fault barrier socket metadata is unsafe")
"""

_FAULT_BARRIER_CLIENT = """
import socket
import sys

path = "/run/kdive/provider-authority/proof-control/control.sock"
request = sys.stdin.buffer.read(1025)
if not request or len(request) > 1024:
    raise SystemExit("fault barrier request is unsafe")
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    connection.settimeout(10)
    connection.connect(path)
    connection.sendall(len(request).to_bytes(4, "big") + request)
    header = bytearray()
    while len(header) < 4:
        chunk = connection.recv(4 - len(header))
        if not chunk:
            raise SystemExit("fault barrier response is incomplete")
        header.extend(chunk)
    size = int.from_bytes(header, "big")
    if size > 1024:
        raise SystemExit("fault barrier response is oversized")
    response = bytearray()
    while len(response) < size:
        chunk = connection.recv(size - len(response))
        if not chunk:
            raise SystemExit("fault barrier response is incomplete")
        response.extend(chunk)
finally:
    connection.close()
sys.stdout.buffer.write(response)
"""


class RemoteAuthorityProofConfig(BaseModel):
    """Non-secret operator inputs for one fixed remote authority proof host."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    installed_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    project: Annotated[str, Field(min_length=1, max_length=63)]
    resource_name: Annotated[
        str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    ]
    authority_instance: Annotated[
        str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    ]
    ownership_prefix: str
    ssh_target: Annotated[
        str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
    ]
    authority_service: Literal["kdive-external-boot-authority.service"]
    barrier_socket: Path | None = None

    @field_validator("ownership_prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        if _PREFIX.fullmatch(value) is None:
            raise ValueError("ownership prefix must be unique and bounded")
        return value

    @field_validator("barrier_socket")
    @classmethod
    def validate_barrier(cls, value: Path | None) -> Path | None:
        if value is not None and value != _PROOF_SOCKET:
            raise ValueError("barrier socket must be the fixed authority proof socket")
        return value


def load_config(
    environment: dict[str, str] | None = None,
) -> RemoteAuthorityProofConfig | None:
    """Return ``None`` only when the remote carrier trigger is absent."""
    env = os.environ if environment is None else environment
    raw = env.get(CONFIG_ENV)
    if raw is None:
        return None
    descriptor = os.open(
        Path(raw),
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) not in (0o400, 0o600)
            or not 0 < metadata.st_size <= _MAX_CONFIG_BYTES
        ):
            raise ValueError("remote authority proof config has unsafe metadata")
        data = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > _MAX_CONFIG_BYTES:
        raise ValueError("remote authority proof config exceeds 16384 bytes")
    return RemoteAuthorityProofConfig.model_validate_json(data)


def _ssh(
    config: RemoteAuthorityProofConfig,
    command: tuple[str, ...],
    *,
    payload: bytes = b"",
) -> bytes:
    stdout, stderr, returncode = _run_bounded_command(
        (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            config.ssh_target,
            shlex.join(command),
        ),
        payload=payload,
        timeout=15,
        stdout_limit=_MAX_CONTROL_BYTES,
        stderr_limit=_MAX_CONTROL_BYTES,
    )
    if returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"remote authority control failed: {detail}")
    return stdout


def _run_bounded_command(
    argv: tuple[str, ...],
    *,
    payload: bytes,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> tuple[bytes, bytes, int]:
    """Run one fixed command with bounded pipes and completion-owned cleanup."""
    process = subprocess.Popen(  # noqa: S603 - fixed SSH or test argv only
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        _terminate_process_group(process)
        raise RuntimeError("bounded remote control did not expose all pipes")
    stdout = bytearray()
    stderr = bytearray()
    streams = (
        (process.stdout, "stdout", stdout_limit, stdout),
        (process.stderr, "stderr", stderr_limit, stderr),
    )
    selector = selectors.DefaultSelector()
    try:
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            process.stdin.close()
        for stream, name, limit, output in streams:
            selector.register(stream, selectors.EVENT_READ, (name, limit, output, stream))
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            ready = selector.select(remaining)
            if not ready:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in ready:
                name, limit, output, stream = cast(tuple[str, int, bytearray, IO[bytes]], key.data)
                chunk = os.read(key.fd, min(65_536, limit + 1 - len(output)))
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                output.extend(chunk)
                if len(output) > limit:
                    raise AssertionError(f"remote authority control {name} is oversized")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        returncode = process.wait(timeout=remaining)
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        selector.close()
        with suppress(BrokenPipeError):
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()
    return (
        bytes(stdout),
        bytes(stderr),
        returncode,
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the child session and synchronously reap its leader."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(ProcessLookupError):
        process.kill()
    process.wait()


def require_fault_barrier(config: RemoteAuthorityProofConfig) -> Path:
    """Require the fixed owner-only ADR-0622 socket on the configured host."""
    if config.barrier_socket is None:
        raise RuntimeError("installed authority exposes no deterministic provider-effect barrier")
    try:
        _ssh(config, ("sudo", "-n", _AUTHORITY_PYTHON, "-c", _FAULT_BARRIER_METADATA))
    except RuntimeError:
        raise RuntimeError(
            "installed authority exposes no deterministic provider-effect barrier"
        ) from None
    return config.barrier_socket


def fault_barrier_request(
    config: RemoteAuthorityProofConfig, request: dict[str, str]
) -> dict[str, str]:
    """Send one bounded closed proof request to the fixed remote authority socket."""
    payload = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if not payload or len(payload) > _MAX_CONTROL_BYTES:
        raise ValueError("fault barrier request exceeds its closed byte limit")
    require_fault_barrier(config)
    stdout = _ssh(
        config,
        ("sudo", "-n", _AUTHORITY_PYTHON, "-c", _FAULT_BARRIER_CLIENT),
        payload=payload,
    )
    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        raise AssertionError("remote authority fault barrier response is malformed") from None
    if not isinstance(response, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in response.items()
    ):
        raise AssertionError("remote authority fault barrier response is malformed")
    return response


def restart_authority_after_fault(config: RemoteAuthorityProofConfig) -> None:
    """Restart only the installed authority unit on the fixed proof host."""
    _ssh(config, ("sudo", "-n", "systemctl", "restart", config.authority_service))
    status = _ssh(config, ("systemctl", "is-active", config.authority_service))
    if status != b"active\n":
        raise AssertionError("remote authority service did not return active after restart")
