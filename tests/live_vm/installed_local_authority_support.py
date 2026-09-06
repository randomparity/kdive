"""Closed configuration and cleanup scope for the #2151 native carrier."""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import re
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
import pytest
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kdive.mcp.dev_harness import LiveStackClient
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.skew import _fetch_version, _resolve, readyz_urls
from tests.integration.live_stack.spine import (
    build_and_upload_kernel,
    drain_job,
    mint_role_token,
    ok,
    scalar,
)

CONFIG_ENV = "KDIVE_LIVE_VM_LOCAL_AUTHORITY_CONFIG"
OPERATIONS = ("activate", "recover", "resolve-conflict", "release", "cleanup", "teardown")
_PREFIX = re.compile(r"kdive-2151-[0-9a-f]{12}-[0-9a-f]{8}")
_FIXED_WORKER_UNIT = re.compile(r"kdive-live-worker@([1-8])\.service")
_AUTHORITY_ACCOUNT = "kdive-provider-authority"
_AUTHORITY_ARTIFACT_ROOTS = (
    Path("/var/lib/kdive/provider-authority/rootfs"),
    Path("/var/lib/kdive/provider-authority/console"),
)
_IDENTITY_PYTHON = "/usr/bin/python3"
_FAULT_BARRIER_MAX_BYTES = 1024
_FAULT_BARRIER_SOCKET = Path("/run/kdive/provider-authority/proof-control/control.sock")

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
import json
import socket
import sys

path = sys.argv[1]
request = sys.stdin.buffer.read()
if not request or len(request) > 1024:
    raise SystemExit("fault barrier request is unsafe")
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    connection.settimeout(10)
    connection.connect(path)
    connection.sendall(len(request).to_bytes(4, "big") + request)
    header = connection.recv(4)
    if len(header) != 4:
        raise SystemExit("fault barrier response is incomplete")
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

_CREATE_SENTINELS = """
import json
import os
import stat
import sys

for entry in json.load(sys.stdin):
    metadata = os.stat(entry["artifact"], follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"authority fixture artifact is not a regular file: {entry['artifact']}")
    for name in (entry["sentinel"], entry["replacement"]):
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        os.close(descriptor)
"""

_REMOVE_SENTINELS = """
import json
import os
import sys

for entry in json.load(sys.stdin):
    for name in (entry["sentinel"], entry["replacement"]):
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
"""

_IDENTITY_BYPASS_PROBE = """
import json
import os
import sys

failures = []
for entry in json.load(sys.stdin):
    try:
        descriptor = os.open(entry["artifact"], os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        pass
    else:
        os.close(descriptor)
        failures.append(f"opened private artifact {entry['artifact']}")
    try:
        os.unlink(entry["sentinel"])
    except OSError:
        pass
    else:
        failures.append(f"unlinked authority sentinel {entry['sentinel']}")
    try:
        os.replace(entry["replacement"], entry["sentinel"])
    except OSError:
        pass
    else:
        failures.append(f"replaced authority sentinel {entry['sentinel']}")
if failures:
    raise SystemExit("; ".join(failures))
"""


class NativeAuthorityConfig(BaseModel):
    """Non-secret operator inputs for one installed-authority invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    installed_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    system_id: UUID
    project: Annotated[str, Field(min_length=1, max_length=63)]
    ownership_prefix: str
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
        if value is None:
            return None
        if value != _FAULT_BARRIER_SOCKET:
            raise ValueError("barrier socket must be the fixed authority proof socket")
        return value


@dataclass(frozen=True, slots=True)
class NormalOperationJobs:
    """The public operations whose exact durable completion the carrier verifies."""

    investigation_id: str
    run_id: str
    activate_job_id: str
    release_job_id: str


@dataclass(frozen=True, slots=True)
class ActivationJob:
    """One publicly admitted activation whose job has not yet been drained."""

    investigation_id: str
    run_id: str
    activate_job_id: str


def _output(*argv: str) -> str:
    result = subprocess.run(argv, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def run_installed_local_authority_normal_operations() -> None:
    """Run the configured carrier's public activate and root-release proof.

    The collected native-test entrypoint remains deliberately thin; this support function also
    keeps pre-mutation, confinement, and cleanup behavior directly unit-testable without importing
    a collected test module.
    """
    config = load_config()
    if config is None:
        pytest.skip("installed local-authority carrier is not configured")

    installed = _output("sudo", "-n", "cat", "/opt/kdive-provider-authority/revision")
    assert installed == config.installed_revision, (
        f"installed authority revision {installed!r} does not match configured coherent revision"
    )
    assert _output("systemctl", "is-active", config.authority_service) == "active"
    running_workers = _output(
        "systemctl",
        "list-units",
        "kdive-live-worker@*.service",
        "--state=running",
        "--no-legend",
    )

    issuer = require_issuer()
    base_url = require_stack()
    require_deployed_revision(config, base_url, running_workers)
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    assert db_url, "native authority carrier requires KDIVE_DATABASE_URL"
    token = mint_role_token(
        issuer,
        project=config.project,
        agent_session=config.ownership_prefix,
        role="admin",
    )
    ledger = ResourceLedger(config.ownership_prefix)

    async def run() -> None:
        await provision_authority_fixture(db_url, config)
        require_authority_artifact_confinement(config, running_workers)
        client = LiveStackClient.over_http(base_url, token)
        async with client:
            primary: Exception | None = None
            try:
                operations = await drive_normal_operations(client, config, ledger)
                await assert_root_release_completion(db_url, operations)
            except Exception as exc:  # preserve the native failure while still attempting cleanup
                primary = exc
            cleanup_failures: list[Exception] = []
            investigations = [r for r in ledger.resources if r.kind == "investigation"]
            for resource in reversed(investigations):
                try:
                    closed = await client.call_tool(
                        "investigations.close",
                        investigation_id=resource.identity,
                        summary="Native authority proof cleanup",
                    )
                    assert not isinstance(closed, list)
                    assert closed.status not in {"error", "failed"}
                except Exception as exc:
                    cleanup_failures.append(exc)
            if primary is not None:
                cleanup_failures.insert(0, primary)
            if len(cleanup_failures) == 1:
                raise cleanup_failures[0]
            if cleanup_failures:
                raise ExceptionGroup("native carrier and cleanup failures", cleanup_failures)

    asyncio.run(run())


def run_installed_local_authority_restart_recovery() -> None:
    """Prove a real authority restart after provider effect, before its journal return record."""
    config = load_config()
    if config is None:
        pytest.skip("installed local-authority carrier is not configured")
    require_fault_barrier(config)

    installed = _output("sudo", "-n", "cat", "/opt/kdive-provider-authority/revision")
    assert installed == config.installed_revision, (
        f"installed authority revision {installed!r} does not match configured coherent revision"
    )
    assert _output("systemctl", "is-active", config.authority_service) == "active"
    running_workers = _output(
        "systemctl",
        "list-units",
        "kdive-live-worker@*.service",
        "--state=running",
        "--no-legend",
    )

    issuer = require_issuer()
    base_url = require_stack()
    require_deployed_revision(config, base_url, running_workers)
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    assert db_url, "native authority carrier requires KDIVE_DATABASE_URL"
    token = mint_role_token(
        issuer,
        project=config.project,
        agent_session=config.ownership_prefix,
        role="admin",
    )
    ledger = ResourceLedger(config.ownership_prefix)

    async def run() -> None:
        await provision_authority_fixture(db_url, config)
        require_authority_artifact_confinement(config, running_workers)
        client = LiveStackClient.over_http(base_url, token)
        async with client:
            primary: Exception | None = None
            try:
                activation = await start_external_boot_activation(
                    client,
                    config,
                    ledger,
                    before_activate=lambda run_id: arm_fault_barrier(
                        config, run_id, "activate", "after-provider"
                    ),
                )
                wait_for_fault_barrier(config)
                restart_authority_after_fault(config)
                await drain_job(client, "activate-restart-recovery", activation.activate_job_id)
                release = ok(
                    await scalar(client, "runs.release_external_boot", run_id=activation.run_id),
                    "release",
                )
                await drain_job(client, "release", release.object_id)
                await assert_root_release_completion(
                    db_url,
                    NormalOperationJobs(
                        investigation_id=activation.investigation_id,
                        run_id=activation.run_id,
                        activate_job_id=activation.activate_job_id,
                        release_job_id=release.object_id,
                    ),
                )
            except Exception as exc:  # preserve the native failure while still attempting cleanup
                primary = exc
            cleanup_failures: list[Exception] = []
            investigations = [
                resource for resource in ledger.resources if resource.kind == "investigation"
            ]
            for resource in reversed(investigations):
                try:
                    closed = await client.call_tool(
                        "investigations.close",
                        investigation_id=resource.identity,
                        summary="Native authority proof cleanup",
                    )
                    assert not isinstance(closed, list)
                    assert closed.status not in {"error", "failed"}
                except Exception as exc:
                    cleanup_failures.append(exc)
            if primary is not None:
                cleanup_failures.insert(0, primary)
            if len(cleanup_failures) == 1:
                raise cleanup_failures[0]
            if cleanup_failures:
                raise ExceptionGroup("native carrier and cleanup failures", cleanup_failures)

    asyncio.run(run())


def require_deployed_revision(
    config: NativeAuthorityConfig,
    base_url: str,
    running_workers: str,
    *,
    fetch: Callable[[str], dict[str, object] | None] = _fetch_version,
    resolve: Callable[[str], str | None] = _resolve,
) -> None:
    """Refuse carrier mutation unless reports resolve exactly to the configured full build."""
    default_urls = readyz_urls(base_url, {})
    server_url = default_urls["server"]
    worker_urls = _active_worker_readyz_urls(base_url, running_workers)
    for process, url in (("server", server_url), *worker_urls):
        version = fetch(url)
        commit = version.get("commit") if version is not None else None
        resolved = resolve(commit) if isinstance(commit, str) else None
        if resolved != config.installed_revision:
            reported = commit if isinstance(commit, str) else "unknown"
            raise AssertionError(
                f"deployed {process} revision {reported!r} does not match configured "
                f"installed revision {config.installed_revision}"
            )


def _active_worker_readyz_urls(base_url: str, running_workers: str) -> tuple[tuple[str, str], ...]:
    """Derive fixed-slot aux URLs from the lifecycle provisioner's assigned bind map."""
    slots = sorted({int(slot) for slot in _FIXED_WORKER_UNIT.findall(running_workers)})
    if not slots:
        raise AssertionError("native authority carrier requires an active fixed worker incarnation")
    parsed = urlsplit(base_url)
    host = parsed.hostname
    if host is None:
        raise AssertionError("native authority carrier stack URL has no host")
    authority = f"[{host}]" if ":" in host else host
    scheme = parsed.scheme or "http"
    return tuple(
        (
            f"worker slot {slot}",
            f"{scheme}://{authority}:{9465 if slot == 1 else 9468 + slot}/readyz",
        )
        for slot in slots
    )


def _active_worker_slots(running_workers: str) -> tuple[int, ...]:
    """Return the exact fixed-worker slots systemd reports as active for this invocation."""
    slots = tuple(sorted({int(slot) for slot in _FIXED_WORKER_UNIT.findall(running_workers)}))
    if not slots:
        raise AssertionError("native authority carrier requires an active fixed worker incarnation")
    return slots


def _authority_artifacts(config: NativeAuthorityConfig) -> tuple[tuple[str, Path], ...]:
    """Construct only the selected fixture's two authority-private artifacts."""
    return (
        ("overlay", _AUTHORITY_ARTIFACT_ROOTS[0] / f"{config.system_id}-overlay.qcow2"),
        ("console", _AUTHORITY_ARTIFACT_ROOTS[1] / f"{config.system_id}.log"),
    )


def _sentinel_entries(config: NativeAuthorityConfig) -> tuple[dict[str, str], ...]:
    """Name the only new files this invocation may create beneath private artifact parents."""
    return tuple(
        {
            "artifact": str(artifact),
            "sentinel": str(artifact.parent / f".{config.ownership_prefix}-{kind}-sentinel"),
            "replacement": str(artifact.parent / f".{config.ownership_prefix}-{kind}-replacement"),
        }
        for kind, artifact in _authority_artifacts(config)
    )


def _run_identity_program(
    identity: str,
    program: str,
    entries: tuple[dict[str, str], ...],
    *,
    sudo: bool,
) -> None:
    argv = [_IDENTITY_PYTHON, "-c", program]
    if sudo:
        argv = ["sudo", "-n", "-u", identity, *argv]
    result = subprocess.run(
        argv,
        input=json.dumps(entries),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise AssertionError(f"identity {identity} bypass probe failed: {detail}")


def require_authority_artifact_confinement(
    config: NativeAuthorityConfig,
    running_workers: str,
) -> None:
    """Require every active worker and this control uid to be unable to alter private artifacts.

    This is native-only evidence: the subprocesses run under installed identities. Unit tests mock
    only that subprocess boundary and do not establish host permission enforcement.
    """
    entries = _sentinel_entries(config)
    _run_identity_program(_AUTHORITY_ACCOUNT, _CREATE_SENTINELS, entries, sudo=True)
    try:
        for slot in _active_worker_slots(running_workers):
            _run_identity_program(
                f"kdive-worker-{slot}", _IDENTITY_BYPASS_PROBE, entries, sudo=True
            )
        control_identity = pwd.getpwuid(os.geteuid()).pw_name
        _run_identity_program(control_identity, _IDENTITY_BYPASS_PROBE, entries, sudo=False)
    finally:
        _run_identity_program(_AUTHORITY_ACCOUNT, _REMOVE_SENTINELS, entries, sudo=True)


def load_config(environment: dict[str, str] | None = None) -> NativeAuthorityConfig | None:
    """Return ``None`` only when the native carrier trigger is absent."""
    env = os.environ if environment is None else environment
    raw = env.get(CONFIG_ENV)
    if raw is None:
        return None
    path = Path(raw)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in (0o400, 0o600)
            or not 0 < metadata.st_size <= 16_384
        ):
            raise ValueError("local authority proof config has unsafe metadata")
        data = os.read(descriptor, 16_385)
    finally:
        os.close(descriptor)
    if len(data) > 16_384:
        raise ValueError("local authority proof config exceeds 16384 bytes")
    return NativeAuthorityConfig.model_validate_json(data)


class OwnedResource(BaseModel):
    """One exact carrier-created resource eligible for cleanup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal[
        "domain", "volume", "object", "journal", "recovery", "investigation", "run", "activation"
    ]
    identity: Annotated[str, Field(min_length=1, max_length=1024)]


class ResourceLedger:
    """Attempt exact cleanup in reverse creation order without inferred targets."""

    def __init__(self, prefix: str) -> None:
        if _PREFIX.fullmatch(prefix) is None:
            raise ValueError("invalid ownership prefix")
        self.prefix = prefix
        self._resources: list[OwnedResource] = []

    @property
    def resources(self) -> tuple[OwnedResource, ...]:
        return tuple(self._resources)

    def record(self, resource: OwnedResource) -> None:
        if resource in self._resources:
            raise ValueError("owned resource was recorded twice")
        if self.prefix not in resource.identity and resource.kind not in {
            "investigation",
            "run",
            "activation",
        }:
            raise ValueError("resource identity is outside the invocation ownership scope")
        self._resources.append(resource)

    def cleanup(self, remove: Callable[[OwnedResource], None]) -> None:
        failures: list[Exception] = []
        for resource in reversed(self._resources):
            try:
                remove(resource)
            except Exception as exc:
                try:
                    raise RuntimeError(f"cleanup failed for {resource.kind}") from exc
                except RuntimeError as wrapped:
                    failures.append(wrapped)
        if failures:
            raise ExceptionGroup("owned-resource cleanup failed", failures)


def require_fault_barrier(config: NativeAuthorityConfig) -> Path:
    """Fail loud rather than representing unavailable deterministic arms as acceptance."""
    if config.barrier_socket is None:
        raise RuntimeError("installed authority exposes no deterministic provider-effect barrier")
    try:
        _output("sudo", "-n", _IDENTITY_PYTHON, "-c", _FAULT_BARRIER_METADATA)
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "installed authority exposes no deterministic provider-effect barrier"
        ) from None
    return config.barrier_socket


def fault_barrier_request(config: NativeAuthorityConfig, request: dict[str, str]) -> dict[str, str]:
    """Send one bounded closed proof request through the root-only installed socket."""
    socket_path = require_fault_barrier(config)
    payload = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if not payload or len(payload) > _FAULT_BARRIER_MAX_BYTES:
        raise ValueError("fault barrier request exceeds its closed byte limit")
    result = subprocess.run(
        ["sudo", "-n", _IDENTITY_PYTHON, "-c", _FAULT_BARRIER_CLIENT, str(socket_path)],
        input=payload,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()[-1000:]
        raise RuntimeError(f"installed authority fault barrier request failed: {detail}")
    if len(result.stdout) > _FAULT_BARRIER_MAX_BYTES:
        raise AssertionError("installed authority fault barrier response is oversized")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError("installed authority fault barrier response is malformed") from None
    if not isinstance(response, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in response.items()
    ):
        raise AssertionError("installed authority fault barrier response is malformed")
    return response


def arm_fault_barrier(
    config: NativeAuthorityConfig,
    run_id: str,
    operation: Literal["activate", "recover", "cleanup"],
    checkpoint: Literal["before-provider", "after-provider"],
) -> None:
    """Arm one exact configured-System provider checkpoint or fail loud."""
    response = fault_barrier_request(
        config,
        {
            "action": "arm",
            "system_id": str(config.system_id),
            "run_id": run_id,
            "operation": operation,
            "checkpoint": checkpoint,
        },
    )
    if response != {"state": "armed"}:
        raise RuntimeError(f"installed authority fault barrier refused arm: {response!r}")


def release_fault_barrier(config: NativeAuthorityConfig) -> None:
    """Release the sole configured-System fault checkpoint or fail loud."""
    response = fault_barrier_request(config, {"action": "release"})
    if response != {"state": "released"}:
        raise RuntimeError(f"installed authority fault barrier refused release: {response!r}")


def wait_for_fault_barrier(config: NativeAuthorityConfig, *, timeout_seconds: float = 60.0) -> None:
    """Wait for the configured checkpoint by its observable state, never elapsed-time luck."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = fault_barrier_request(config, {"action": "status"})
        if response == {"state": "reached"}:
            return
        if response != {"state": "armed"}:
            raise RuntimeError(f"installed authority fault barrier lost its arm: {response!r}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "installed authority provider effect did not reach its armed barrier"
            )
        time.sleep(min(0.1, remaining))


def restart_authority_after_fault(config: NativeAuthorityConfig) -> None:
    """Restart only the configured authority service after a reached native checkpoint."""
    _output("sudo", "-n", "systemctl", "restart", config.authority_service)
    if _output("systemctl", "is-active", config.authority_service) != "active":
        raise AssertionError("authority service did not return active after fault restart")


async def assert_root_release_completion(db_url: str, operations: NormalOperationJobs) -> None:
    """Require the root release job's exact derived recover/cleanup finalizer evidence."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        for job_id, operation in (
            (operations.activate_job_id, "activate"),
            (operations.release_job_id, "release"),
        ):
            await cur.execute(
                "SELECT state, payload->'external_boot_authority_v1'->>'operation', "
                "payload->'external_boot_authority_v1'->>'run_id' FROM jobs WHERE id = %s",
                (job_id,),
            )
            if await cur.fetchall() != [("succeeded", operation, operations.run_id)]:
                raise AssertionError(
                    f"root release completion lacks succeeded {operation} job {job_id}"
                )
        await cur.execute(
            "SELECT activation.state, activation.cleanup_complete, attempt.state, "
            "attempt.authority_generation = root.generation, "
            "attempt.terminal_evidence IS NOT NULL, root.state, root.purpose, root.operation, "
            "root.activation_id = activation.id, root.system_id = activation.system_id, "
            "root.run_id = activation.run_id, root.plan_identity = activation.plan_identity, "
            "root.job_id = receipt.job_id, root.job_attempt = receipt.job_attempt, "
            "receipt.consumed, "
            "receipt.adopted_from_root_authority_id IS NULL, head.phase, "
            "head.authority_id = root.id, head.generation = root.generation, "
            "head.sequence = receipt.journal_sequence, head.digest = receipt.journal_digest, "
            "head.head_record->>'operation', "
            "head.head_record->>'operation_identity' = receipt.operation_identity, "
            "head.head_record->>'operation_digest' = receipt.operation_digest, "
            "head.head_record #>> '{observation,category}', "
            "head.head_record #>> '{observation,composite_state}' = "
            "receipt.observed_absent_digest, "
            "(SELECT count(*) FROM external_boot_reservation_releases AS credit "
            " WHERE credit.activation_id = activation.id), "
            "NOT EXISTS (SELECT 1 FROM external_boot_reservations AS pending "
            "            WHERE pending.activation_id = activation.id) "
            "FROM external_boot_activations AS activation "
            "JOIN external_boot_recovery_attempts AS attempt "
            "  ON attempt.activation_id = activation.id "
            " AND attempt.attempt_id = activation.current_attempt_id "
            "JOIN external_boot_release_cleanup_receipts AS receipt "
            "  ON receipt.activation_id = activation.id "
            "JOIN external_boot_authorities AS root ON root.id = receipt.root_authority_id "
            "JOIN external_boot_authority_journal_heads AS head "
            "  ON head.system_id = root.system_id "
            " AND head.authority_instance = root.authority_instance "
            "WHERE activation.run_id = %s AND receipt.job_id = %s "
            "  AND receipt.run_id = activation.run_id AND receipt.system_id = activation.system_id "
            "  AND receipt.plan_identity = activation.plan_identity",
            (operations.run_id, operations.release_job_id),
        )
        expected = [
            (
                "recovered",
                True,
                "recovered",
                True,
                True,
                "retired",
                "release",
                "release",
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                True,
                "terminal",
                True,
                True,
                True,
                True,
                "cleanup",
                True,
                True,
                "absent",
                True,
                1,
                True,
            )
        ]
        actual = await cur.fetchall()
        if actual != expected:
            raise AssertionError(
                f"root release completion lacks exact derived finalizer evidence: {actual!r}"
            )


async def provision_authority_fixture(db_url: str, config: NativeAuthorityConfig) -> None:
    """Re-provision only the selected disposable System under the installed authority uid."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT provisioning_profile FROM systems WHERE id = %s AND project = %s",
            (config.system_id, config.project),
        )
        row = await cur.fetchone()
    if row is None:
        raise ValueError(
            "selected authority fixture System is absent or belongs to another project"
        )
    script = Path(__file__).resolve().parents[2] / "scripts/live-vm/provision-authority-fixture.py"
    result = subprocess.run(
        [
            "sudo",
            "-n",
            "/opt/kdive-provider-authority/.venv/bin/python",
            str(script),
            str(config.system_id),
        ],
        input=json.dumps(row[0]),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise RuntimeError(f"authority fixture provisioning failed: {detail}")


async def drive_normal_operations(
    client: LiveStackClient,
    config: NativeAuthorityConfig,
    ledger: ResourceLedger,
) -> NormalOperationJobs:
    """Drive activate then release/cleanup through public tools and real job polling.

    ``runs.boot`` is the public activation admission. ``runs.release_external_boot`` is the
    public release admission; its one root release job owns the derived recover and cleanup
    phases, whose durable finalizer evidence the native carrier verifies after polling.
    """
    activation = await start_external_boot_activation(client, config, ledger)
    await drain_job(client, "activate", activation.activate_job_id)
    release = ok(
        await scalar(client, "runs.release_external_boot", run_id=activation.run_id),
        "release",
    )
    await drain_job(client, "release", release.object_id)
    return NormalOperationJobs(
        investigation_id=activation.investigation_id,
        run_id=activation.run_id,
        activate_job_id=activation.activate_job_id,
        release_job_id=release.object_id,
    )


async def start_external_boot_activation(
    client: LiveStackClient,
    config: NativeAuthorityConfig,
    ledger: ResourceLedger,
    *,
    before_activate: Callable[[str], None] | None = None,
) -> ActivationJob:
    """Create, install, and publicly admit one activation without polling its job."""
    opened = ok(
        await scalar(
            client,
            "investigations.open",
            project=config.project,
            title=config.ownership_prefix,
        ),
        "open-investigation",
    )
    investigation_id = opened.object_id
    ledger.record(OwnedResource(kind="investigation", identity=investigation_id))
    created = ok(
        await scalar(
            client,
            "runs.create",
            investigation_id=investigation_id,
            system_id=str(config.system_id),
            build_profile={"schema_version": 1, "arch": "x86_64"},
            label=config.ownership_prefix,
        ),
        "create-run",
    )
    run_id = created.object_id
    ledger.record(OwnedResource(kind="run", identity=run_id))
    await build_and_upload_kernel(client, run_id=run_id)
    install = ok(await scalar(client, "runs.install", run_id=run_id), "install")
    await drain_job(client, "install", install.object_id)
    if before_activate is not None:
        before_activate(run_id)
    activate = ok(await scalar(client, "runs.boot", run_id=run_id), "activate")
    return ActivationJob(
        investigation_id=investigation_id,
        run_id=run_id,
        activate_job_id=activate.object_id,
    )
