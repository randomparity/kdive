"""Shared support helpers for complete-build tests."""

from __future__ import annotations

import gzip
import io
import struct
import tarfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import HeadResult
from kdive.artifacts.uploads import upload_manifest
from kdive.artifacts.uploads.uploads import ManifestEntry
from kdive.build_artifacts.results import BuildOutput, ValidatedUpload
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RESOURCES, RUNS, SYSTEMS
from kdive.domain.capacity.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, Investigation, Run, System
from kdive.mcp.auth import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.runs.complete_build import CompleteBuildFinalizer
from kdive.services.runs.steps import BuildStepResult
from tests.clock import STORE_MTIME
from tests.mcp.systems_support import provisioning_profile as _provisioning_profile

TEST_DT = datetime(2026, 1, 1, tzinfo=UTC)


def valid_combined_kernel_tar() -> bytes:
    """Return a minimal structurally valid external x86 kernel bundle."""
    build_id = bytes.fromhex("0123456789abcdef")
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    elf = bytearray(64 + 112 + len(note) + 32)
    elf[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", elf, 0x12, 62)
    struct.pack_into("<Q", elf, 0x20, 64)
    struct.pack_into("<H", elf, 0x36, 56)
    struct.pack_into("<H", elf, 0x38, 2)
    note_offset = 64 + 112
    struct.pack_into("<I", elf, 64, 4)
    struct.pack_into("<Q", elf, 64 + 8, note_offset)
    struct.pack_into("<Q", elf, 64 + 32, len(note))
    load_offset = note_offset + len(note)
    struct.pack_into("<I", elf, 120, 1)
    struct.pack_into("<Q", elf, 120 + 8, load_offset)
    struct.pack_into("<Q", elf, 120 + 32, len(elf) - load_offset)
    elf[note_offset : note_offset + len(note)] = note
    elf[load_offset:] = b"Linux version 6.9.0 test\x00".ljust(len(elf) - load_offset, b"\x00")
    boot = bytearray(0x400)
    boot[0x202:0x206] = b"HdrS"
    struct.pack_into("<H", boot, 0x20E, 0x100)
    boot[0x300:0x306] = b"6.9.0\x00"
    boot.extend(gzip.compress(elf))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, data in (
            ("boot/vmlinuz", bytes(boot)),
            ("lib/modules/6.9.0/modules.dep", b""),
            ("lib/modules/6.9.0/kernel/drivers/foo.ko", b"\x7fELFmod"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buf.getvalue()


def ctx(role: Role = Role.OPERATOR) -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": role}
    )


@asynccontextmanager
async def pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    conn_pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await conn_pool.open()
    try:
        yield conn_pool
    finally:
        await conn_pool.close()


async def seed_system(conn_pool: AsyncConnectionPool) -> UUID:
    async with conn_pool.connection() as conn:
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="local-libvirt",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.ACTIVE,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile=_provisioning_profile(),
            ),
        )
    return system.id


async def seed_investigation(conn_pool: AsyncConnectionPool) -> UUID:
    async with conn_pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                title="seeded",
                state=InvestigationState.ACTIVE,
            ),
        )
    return inv.id


async def seed_run(
    conn_pool: AsyncConnectionPool,
    build_profile: dict[str, Any],
    target_kind: ResourceKind = ResourceKind.LOCAL_LIBVIRT,
) -> UUID:
    inv_id = await seed_investigation(conn_pool)
    sys_id = await seed_system(conn_pool)
    async with conn_pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                investigation_id=inv_id,
                system_id=sys_id,
                target_kind=target_kind,
                state=RunState.CREATED,
                build_profile=build_profile,
            ),
        )
    return run.id


async def seed_external_run(
    conn_pool: AsyncConnectionPool,
    build_profile: dict[str, Any] | None = None,
    target_kind: ResourceKind = ResourceKind.LOCAL_LIBVIRT,
) -> UUID:
    """A CREATED external Run with no upload manifest."""
    return await seed_run(conn_pool, build_profile or {"schema_version": 1}, target_kind)


async def seed_external_run_with_manifest(
    conn_pool: AsyncConnectionPool,
    entries: list[ManifestEntry] | None = None,
    build_profile: dict[str, Any] | None = None,
    ttl: timedelta = timedelta(hours=1),
    target_kind: ResourceKind = ResourceKind.LOCAL_LIBVIRT,
) -> UUID:
    """A CREATED external Run plus a persisted upload manifest.

    A negative ``ttl`` seeds an already-lapsed upload window (the deadline is stamped
    ``now() + ttl`` in Postgres), which is how the expiry rejections are exercised.
    """
    run_id = await seed_external_run(conn_pool, build_profile, target_kind)
    async with conn_pool.connection() as conn:
        await upload_manifest.replace_manifest(
            conn,
            upload_manifest.UploadManifestReplaceRequest(
                owner_kind="runs",
                owner_id=run_id,
                prefix=f"local/runs/{run_id}/",
                entries=entries or [ManifestEntry("kernel", "c", 1)],
                ttl=ttl,
            ),
        )
    return run_id


async def run_by_id(conn_pool: AsyncConnectionPool, run_id: Any) -> Run:
    """Load a seeded Run and fail the test when it is unexpectedly absent."""
    async with conn_pool.connection() as conn:
        run = await RUNS.get(conn, run_id)
    assert run is not None
    return run


async def complete_build(
    conn_pool: AsyncConnectionPool,
    run_id: Any,
    finalizer: CompleteBuildFinalizer,
) -> BuildStepResult:
    """Finalize a build with the standard complete-build request context."""
    run = await run_by_id(conn_pool, run_id)
    async with conn_pool.connection() as conn:
        return await finalizer.complete(conn, ctx(), run, build_id=None, cmdline="console=ttyS0")


def build_output(run_id: Any) -> BuildOutput:
    """Return the standard external-build output for a Run."""
    return BuildOutput(f"local/runs/{run_id}/kernel", "", "build-id")


class FakeValidator:
    def __init__(self, output: BuildOutput | Exception) -> None:
        self._output = output
        self.calls = 0
        self.last_arch: str | None = None

    def __call__(
        self,
        manifest,
        keys,
        declared_build_id,
        *,
        arch: str = "x86_64",
    ) -> ValidatedUpload:
        _ = (manifest, declared_build_id)
        self.calls += 1
        self.last_arch = arch
        if isinstance(self._output, Exception):
            raise self._output
        heads = {
            name: HeadResult(
                size_bytes=1,
                checksum_sha256="c",
                etag="e",
                last_modified=STORE_MTIME,
                version_id="test-version",
            )
            for name in keys
        }
        return ValidatedUpload(output=self._output, heads=heads)
