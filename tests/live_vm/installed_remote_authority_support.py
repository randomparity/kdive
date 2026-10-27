"""Closed installed remote-authority proof inputs, fixed SSH controls and the private observer."""

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
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_ENV = "KDIVE_LIVE_VM_REMOTE_AUTHORITY_CONFIG"
_PREFIX = re.compile(r"kdive-2151-[0-9a-f]{12}-[0-9a-f]{8}")
_PROOF_SOCKET = Path("/run/kdive/provider-authority/proof-control/control.sock")
_AUTHORITY_PYTHON = "/usr/bin/python3"
# The observer and the revision read run as the authority account, never root: the account owns
# its interpreter tree, so root running it would hand that account root on the proof host.
_AUTHORITY_ACCOUNT = "kdive-provider-authority"
_AUTHORITY_VENV_PYTHON = "/opt/kdive-provider-authority/.venv/bin/python"
_REVISION = "/opt/kdive-provider-authority/revision"
_MAX_CONFIG_BYTES = 16_384
_MAX_CONTROL_BYTES = 1024
_MAX_INVENTORY_BYTES = 1_048_576

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

# The authority account runs this with its interpreter in isolated mode; its only argument is a
# UUID. The pool name comes from the installed authority environment, with the authority's own
# default and name pattern (kdive.providers.external_boot_authority.host.AuthorityHostConfig).
_INVENTORY = """
import json
import os
import re
import sys
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring

if len(sys.argv) > 2:
    raise SystemExit("inventory takes at most one System UUID")
name = "kdive-" + str(UUID(sys.argv[1])) if len(sys.argv) == 2 else None
pool_name = "default"
with open("/etc/kdive/provider-authority.env", encoding="utf-8") as environment:
    for line in environment:
        key, _, value = line.rstrip("\\n").partition("=")
        if key == "KDIVE_REMOTE_LIBVIRT_STORAGE_POOL":
            pool_name = value
if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", pool_name) is None:
    raise SystemExit("installed authority storage pool name is invalid")
conn = libvirt.open("qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock")
if conn is None:
    raise SystemExit("authority daemon connection returned no handle")
try:
    domains = sorted(domain.name() for domain in conn.listAllDomains(0))
    pool = conn.storagePoolLookupByName(pool_name)
    pool.refresh(0)
    volumes = sorted(pool.listVolumes())
    system = None
    if name in domains:
        domain = conn.lookupByName(name)
        kind = fromstring(domain.XMLDesc(0)).get("type", "")
        system = {"active": domain.isActive() == 1, "type": kind}
finally:
    conn.close()
print(json.dumps({"arch": os.uname().machine, "domains": domains, "volumes": volumes,
                  "system": system}))
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
    limit: int = _MAX_CONTROL_BYTES,
    timeout: float = 15,
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
        timeout=timeout,
        stdout_limit=limit,
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
    """Terminate the child session and synchronously reap its leader.

    macOS fails ``killpg`` with EPERM, not ESRCH, once every group member is a zombie.
    """
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, sig)
        with suppress(ProcessLookupError):
            process.kill()
    finally:
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


@dataclass(frozen=True)
class RemoteDomain:
    """The System's domain on the remote authority daemon: running, and its libvirt type."""

    active: bool
    kind: str


@dataclass(frozen=True)
class RemoteAuthorityInventory:
    """The remote authority's host architecture, domains, private pool volumes and one System."""

    arch: str
    domains: list[str]
    volumes: list[str]
    system: RemoteDomain | None


def _names(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise AssertionError("remote authority inventory is malformed")
    return [str(name) for name in value]


def _remote_domain(value: object) -> RemoteDomain | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"active", "type"}:
        raise AssertionError("remote authority inventory is malformed")
    active, kind = value.get("active"), value.get("type")
    if not isinstance(active, bool) or not isinstance(kind, str):
        raise AssertionError("remote authority inventory is malformed")
    return RemoteDomain(active, kind)


def run_as_authority(config: RemoteAuthorityProofConfig, script: str, *args: str) -> bytes:
    """Run one fixed proof script as the remote authority account, over the operator's SSH."""
    return _ssh(
        config,
        ("sudo", "-n", "-u", _AUTHORITY_ACCOUNT, _AUTHORITY_VENV_PYTHON, "-I", "-c", script, *args),
        timeout=120,
    )


def remote_authority_inventory(
    config: RemoteAuthorityProofConfig, system_id: str | None = None
) -> RemoteAuthorityInventory:
    """Read the remote authority's private daemon and pool as its account; name one System."""
    command: tuple[str, ...] = (
        "sudo",
        "-n",
        "-u",
        _AUTHORITY_ACCOUNT,
        _AUTHORITY_VENV_PYTHON,
        "-I",
        "-c",
        _INVENTORY,
    )
    if system_id is not None:
        command += (str(UUID(system_id)),)
    stdout = _ssh(config, command, limit=_MAX_INVENTORY_BYTES, timeout=120)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise AssertionError("remote authority inventory is malformed") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"arch", "domains", "volumes", "system"}
        or not isinstance(data["arch"], str)
    ):
        raise AssertionError("remote authority inventory is malformed")
    return RemoteAuthorityInventory(
        data["arch"],
        _names(data["domains"]),
        _names(data["volumes"]),
        _remote_domain(data["system"]),
    )


def require_installed_authority(config: RemoteAuthorityProofConfig) -> None:
    """Refuse unless the remote authority unit is active at the configured revision."""
    if _ssh(config, ("systemctl", "is-active", config.authority_service)) != b"active\n":
        raise AssertionError("the remote authority service is not active")
    revision = _ssh(config, ("sudo", "-n", "-u", _AUTHORITY_ACCOUNT, "cat", _REVISION)).strip()
    if revision != config.installed_revision.encode():
        raise AssertionError("the installed remote authority revision does not match the config")
