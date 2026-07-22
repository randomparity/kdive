"""Tests for console-part + sidecar reclaim at System teardown (local rotation, #892).

Drives ``teardown_handler`` against a migrated Postgres connection and an in-memory object store.
The console-rotation parts and their ``artifacts`` rows expire via the artifact reconciler (#768),
but the rotation-state sidecar has no row, so only an explicit teardown delete reclaims it — these
tests pin that the sidecar object is gone, the ``console-part-*`` rows are gone, and that a deletion
failure or an empty System still lets teardown succeed (best-effort).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import (
    ArtifactWriteRequest,
    FetchedArtifact,
    HeadResult,
    StoredArtifact,
)
from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import AllocationState, JobState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers import systems as systems_handlers
from kdive.jobs.handlers.console import console_rotate
from kdive.providers.console_parts.sidecar import sidecar_object_name
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore
from tests.adversarial.conftest import seed_allocation, seed_resource
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_CONSOLE = b"console-line payload bytes\n" * 6000  # ~158 KiB -> several 64 KiB parts


class _FakeStore:
    """In-memory object store with put/get/head/delete for the rotation + reclaim seam."""

    def __init__(self, *, fail_delete: bool = False) -> None:
        self.objects: dict[str, tuple[bytes, Sensitivity, str, str | None]] = {}
        self.deleted: list[str] = []
        self._fail_delete = fail_delete

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        key = request.key()
        self.objects[key] = (
            request.data,
            request.sensitivity,
            request.retention_class,
            request.content_encoding,
        )
        etag = hashlib.sha256(request.data).hexdigest()
        return StoredArtifact(key, etag, request.sensitivity, request.retention_class)

    def get_artifact(self, key: str, _etag: str | None) -> FetchedArtifact:
        if key not in self.objects:
            raise KeyError(key)
        data, sensitivity, retention, _enc = self.objects[key]
        return FetchedArtifact(data, sensitivity, retention)

    def head(self, key: str) -> HeadResult | None:
        if key not in self.objects:
            return None
        data, sensitivity, _retention, enc = self.objects[key]
        return HeadResult(
            size_bytes=len(data),
            checksum_sha256=None,
            etag="etag",
            sensitivity=sensitivity,
            content_encoding=enc,
        )

    def delete(self, key: str) -> None:
        if self._fail_delete:
            raise CategorizedError(
                f"object-store delete for {key!r} failed: simulated outage",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"key": key},
            )
        self.deleted.append(key)
        self.objects.pop(key, None)


def _console_job(system_id: UUID, boot_id: str) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.CONSOLE_ROTATE,
        payload={"system_id": str(system_id), "boot_id": boot_id},
        state=JobState.RUNNING,
        max_attempts=1,
        authorizing={"principal": "reconciler", "agent_session": None, "project": "local"},
        dedup_key=f"console_rotate:{system_id}",
    )


def _teardown_job(system_id: UUID) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.TEARDOWN,
        payload={"system_id": str(system_id)},
        state=JobState.RUNNING,
        max_attempts=1,
        authorizing={"principal": "alice", "agent_session": "s", "project": "proj"},
        dedup_key=f"teardown:{system_id}",
    )


class _Provisioner:
    """A teardown-only fake provider; records the reaped domain name."""

    def __init__(self) -> None:
        self.torn_down: list[str] = []

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)


async def _seed_ready_system(pool: AsyncConnectionPool) -> UUID:
    async with pool.connection() as conn:
        resource = await seed_resource(conn, cap=4)
        allocation = await seed_allocation(conn, resource.id, AllocationState.ACTIVE)
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                agent_session="s",
                project="proj",
                allocation_id=allocation.id,
                state=SystemState.READY,
                provisioning_profile={},
            ),
        )
    return system.id


async def _rotate(pool: AsyncConnectionPool, store: _FakeStore, system_id: UUID) -> None:
    async with pool.connection() as conn:
        await console_rotate.console_rotate_handler(
            conn,
            _console_job(system_id, "boot-A"),
            secret_registry=SecretRegistry(),
            artifact_store=cast(ObjectStore, store),
        )


async def _teardown(pool: AsyncConnectionPool, store: _FakeStore, system_id: UUID) -> str:
    prov = _Provisioner()
    resolver = provider_resolver(provisioner=prov)
    async with pool.connection() as conn:
        result = await systems_handlers.teardown_handler(
            conn,
            _teardown_job(system_id),
            resolver=resolver,
            artifact_store=cast(ObjectStore, store),
        )
    assert prov.torn_down, "domain must still be reaped"
    return cast(str, result)


async def _part_rows(pool: AsyncConnectionPool, system_id: UUID) -> list[str]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT object_key FROM artifacts WHERE owner_kind = 'systems' AND owner_id = %s "
            "ORDER BY object_key",
            (system_id,),
        )
        return [row[0] for row in await cur.fetchall()]


def test_teardown_reclaims_console_parts_and_sidecar(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[list[str], list[str], list[str], bool]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool)
            log = tmp_path / f"{system_id}.log"
            log.write_bytes(_CONSOLE)
            monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)
            store = _FakeStore()
            await _rotate(pool, store, system_id)
            before = await _part_rows(pool, system_id)
            sidecar_key = f"local/systems/{system_id}/{sidecar_object_name()}"
            assert sidecar_key in store.objects, "rotation must have written a sidecar"
            await _teardown(pool, store, system_id)
            after = await _part_rows(pool, system_id)
            objects_after = [k for k in store.objects if "console-part-" in k]
            return before, after, objects_after, sidecar_key in store.objects

    before, after, objects_after, sidecar_present = asyncio.run(_run())

    assert any("console-part-" in key for key in before), "rotation must seal at least one part row"
    assert [k for k in after if "console-part-" in k] == [], "console-part rows must be reclaimed"
    # The part OBJECTS (not just their rows) are deleted by key — a wrong-key delete leaks them.
    assert objects_after == [], "console-part objects must be deleted from the store"
    assert not sidecar_present, "sidecar object must be deleted at teardown"


async def _seed_sysrq_artifact(
    pool: AsyncConnectionPool, store: _FakeStore, system_id: UUID
) -> str:
    name = f"sysrq-diagnostic-{uuid4()}"
    stored = store.put_artifact(
        ArtifactWriteRequest(
            tenant="local",
            owner_kind="systems",
            owner_id=str(system_id),
            name=name,
            data=b"sysrq dump\n",
            sensitivity=Sensitivity.REDACTED,
            retention_class="console",
        )
    )
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('systems', %s, %s, %s, 'redacted', 'console')",
            (system_id, stored.key, stored.etag),
        )
    return stored.key


def test_teardown_reclaims_sysrq_diagnostic_artifacts(migrated_url: str) -> None:
    async def _run() -> tuple[list[str], list[str], bool]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool)
            store = _FakeStore()
            key = await _seed_sysrq_artifact(pool, store, system_id)
            assert key in store.objects
            before = await _part_rows(pool, system_id)
            await _teardown(pool, store, system_id)
            after = await _part_rows(pool, system_id)
            return before, after, key in store.objects

    before, after, still_present = asyncio.run(_run())

    assert any("sysrq-diagnostic-" in key for key in before), "fixture must seed a sysrq artifact"
    assert [k for k in after if "sysrq-diagnostic-" in k] == [], "sysrq rows must be reclaimed"
    assert not still_present, "sysrq object must be deleted at teardown"


def test_teardown_succeeds_with_nothing_to_clean(migrated_url: str) -> None:
    async def _run() -> tuple[str, list[str], list[str]]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool)
            store = _FakeStore()
            result = await _teardown(pool, store, system_id)
            return result, await _part_rows(pool, system_id), store.deleted

    result, rows, deleted = asyncio.run(_run())

    assert result is not None, "teardown must succeed with no console artifacts present"
    assert rows == []
    # Only the (absent) sidecar delete is attempted; no part objects exist to delete.
    assert all("console-part-" not in key for key in deleted)


def test_teardown_survives_store_delete_failure(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[str, list[str]]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool)
            log = tmp_path / f"{system_id}.log"
            log.write_bytes(_CONSOLE)
            monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)
            await _rotate(pool, _FakeStore(), system_id)
            failing = _FakeStore(fail_delete=True)
            result = await _teardown(pool, failing, system_id)
            # Object delete failed before the row delete, so #768 can still reclaim the rows.
            return result, await _part_rows(pool, system_id)

    result, rows = asyncio.run(_run())

    assert result is not None, "a store delete failure must not fail the teardown job"
    assert any("console-part-" in key for key in rows), "rows survive a failed object delete"
