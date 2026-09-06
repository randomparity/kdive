"""Closed configuration and cleanup scope for the #2151 native carrier."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kdive.mcp.dev_harness import LiveStackClient
from tests.integration.live_stack.spine import build_and_upload_kernel, drain_job, ok, scalar

CONFIG_ENV = "KDIVE_LIVE_VM_LOCAL_AUTHORITY_CONFIG"
OPERATIONS = ("activate", "recover", "resolve-conflict", "release", "cleanup", "teardown")
_PREFIX = re.compile(r"kdive-2151-[0-9a-f]{12}-[0-9a-f]{8}")


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
        if not value.is_absolute():
            raise ValueError("barrier socket must be absolute")
        return value


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
        metadata = config.barrier_socket.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise RuntimeError(
            "installed authority exposes no deterministic provider-effect barrier"
        ) from None
    if not stat.S_ISSOCK(metadata.st_mode):
        raise RuntimeError("installed authority provider-effect barrier is not a socket")
    return config.barrier_socket


async def await_completed_operations(
    db_url: str,
    run_id: str,
    expected: frozenset[str],
    *,
    deadline_s: float = 600.0,
) -> None:
    """Wait until durable jobs prove each expected authority operation succeeded."""
    deadline = time.monotonic() + deadline_s
    while True:
        async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT payload->'external_boot_authority_v1'->>'operation', state "
                "FROM jobs WHERE payload->'external_boot_authority_v1'->>'run_id' = %s",
                (run_id,),
            )
            completed = {
                operation for operation, state in await cur.fetchall() if state == "succeeded"
            }
        if expected <= completed:
            return
        if time.monotonic() >= deadline:
            missing = sorted(expected - completed)
            raise AssertionError(f"authority operations did not succeed before deadline: {missing}")
        await asyncio.sleep(2.0)


async def drive_normal_operations(
    client: LiveStackClient,
    config: NativeAuthorityConfig,
    ledger: ResourceLedger,
) -> tuple[str, str]:
    """Drive activate then release/cleanup through public tools and real job polling.

    ``runs.boot`` is the public activation admission. ``runs.release_external_boot`` is the
    public release admission; its job performs release and the reconciler-created cleanup is
    observed separately by the native test from durable job markers.
    """
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
    activate = ok(await scalar(client, "runs.boot", run_id=run_id), "activate")
    await drain_job(client, "activate", activate.object_id)
    release = ok(
        await scalar(client, "runs.release_external_boot", run_id=run_id),
        "release",
    )
    await drain_job(client, "release", release.object_id)
    return investigation_id, run_id
