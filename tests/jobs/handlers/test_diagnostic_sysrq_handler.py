"""Tests for the diagnostic_sysrq worker job handler (ADR-0285, #925).

Drives ``diagnostic_sysrq_handler`` directly with an in-memory object store and a migrated
Postgres connection. The fake Control port appends the guest's SysRq dump to the console log
file the handler polls, so capture, redaction, no-output, retry idempotency, and the
state-change guard are all exercised without a live guest.
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

from kdive.artifacts.storage import ArtifactWriteRequest, FetchedArtifact, StoredArtifact
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.db.resource_discovery import register_discovered_resource
from kdive.domain.capacity.state import AllocationState, JobState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers import diagnostic_sysrq
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore
from tests.mcp.systems_support import provider_resolver
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeStore:
    """In-memory object store recording every put so idempotency is assertable by key."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, Sensitivity, str]] = {}
        self.put_calls: list[str] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        key = request.key()
        self.objects[key] = (request.data, request.sensitivity, request.retention_class)
        self.put_calls.append(key)
        etag = hashlib.sha256(request.data).hexdigest()
        return StoredArtifact(key, etag, request.sensitivity, request.retention_class)

    def get_artifact(self, key: str, _etag: str | None) -> FetchedArtifact:
        data, sensitivity, retention = self.objects[key]
        return FetchedArtifact(data, sensitivity, retention)


class _FakeControl:
    """Records the injected trigger and appends the guest dump to the console log (or nothing)."""

    def __init__(self, log: Path, dump: bytes) -> None:
        self._log = log
        self._dump = dump
        self.calls: list[tuple[str, str]] = []

    def diagnostic_sysrq(self, domain_name: str, trigger: str) -> None:
        self.calls.append((domain_name, trigger))
        if self._dump:
            with self._log.open("ab") as handle:
                handle.write(self._dump)


async def _seed_ready_system(pool: AsyncConnectionPool, state: SystemState) -> UUID:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: FakeLibvirtConn(), concurrent_allocation_cap=2
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=alloc.id,
                state=state,
                provisioning_profile={},
                domain_name="kdive-x",
            ),
        )
    return system.id


def _job(system_id: UUID, command: str) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.DIAGNOSTIC_SYSRQ,
        payload={"system_id": str(system_id), "command": command},
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "user-1", "agent_session": None, "project": "proj"},
        dedup_key=f"{system_id}:diagnostic_sysrq:{command}:x",
    )


async def _run(
    pool: AsyncConnectionPool,
    store: _FakeStore | None,
    control: _FakeControl,
    job: Job,
    *,
    secret_registry: SecretRegistry | None = None,
) -> str | None:
    resolver = provider_resolver(controller=control)
    async with pool.connection() as conn:
        return await diagnostic_sysrq.diagnostic_sysrq_handler(
            conn,
            job,
            resolver=resolver,
            secret_registry=secret_registry or SecretRegistry(),
            artifact_store=cast(ObjectStore, store) if store is not None else None,
        )


async def _artifact_rows(pool: AsyncConnectionPool, system_id: UUID) -> list[tuple[str, str, str]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT object_key, sensitivity, retention_class FROM artifacts "
            "WHERE owner_kind = 'systems' AND owner_id = %s",
            (system_id,),
        )
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


def _pool(url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(url, min_size=1, max_size=2, open=False)


def test_captures_redacted_artifact_and_returns_its_id(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostic_sysrq, "POLL_INTERVAL_SECONDS", 0.0)
    log = tmp_path / "console.log"
    log.write_bytes(b"boot log\n")
    monkeypatch.setattr(diagnostic_sysrq, "console_log_path", lambda _sid: log)
    control = _FakeControl(log, b"SysRq : Show Blocked State\n task list...\n")

    async def _go() -> tuple[str | None, list[tuple[str, str, str]], list[tuple[str, str]]]:
        async with _pool(migrated_url) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool, SystemState.READY)
            job = _job(system_id, "show_blocked_tasks")
            result_ref = await _run(pool, _FakeStore(), control, job)
            rows = await _artifact_rows(pool, system_id)
            return result_ref, rows, control.calls

    result_ref, rows, calls = asyncio.run(_go())

    assert calls == [("kdive-x", "w")]  # show_blocked_tasks -> trigger 'w'
    assert len(rows) == 1
    object_key, sensitivity, retention = rows[0]
    assert "/sysrq-diagnostic-" in object_key  # object name carries the job id
    assert sensitivity == "redacted"
    assert retention == "console"
    assert result_ref is not None


def test_dump_secret_is_redacted_in_the_stored_object(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostic_sysrq, "POLL_INTERVAL_SECONDS", 0.0)
    log = tmp_path / "console.log"
    log.write_bytes(b"boot\n")
    monkeypatch.setattr(diagnostic_sysrq, "console_log_path", lambda _sid: log)
    control = _FakeControl(log, b"dump password=hunter2 more\n")
    store = _FakeStore()

    async def _go() -> None:
        async with _pool(migrated_url) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool, SystemState.READY)
            await _run(pool, store, control, _job(system_id, "show_memory"))

    asyncio.run(_go())

    stored = b"".join(data for data, _s, _r in store.objects.values())
    assert b"hunter2" not in stored
    assert b"[REDACTED]" in stored


def test_no_console_output_fails_configuration_error(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostic_sysrq, "POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(diagnostic_sysrq, "MAX_POLLS", 2)
    log = tmp_path / "console.log"
    log.write_bytes(b"boot\n")
    monkeypatch.setattr(diagnostic_sysrq, "console_log_path", lambda _sid: log)
    control = _FakeControl(log, b"")  # guest emits nothing

    async def _go() -> None:
        async with _pool(migrated_url) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool, SystemState.READY)
            await _run(pool, _FakeStore(), control, _job(system_id, "show_memory"))

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(_go())
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "no_console_output"


def test_replayed_handler_run_does_not_duplicate_the_row(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostic_sysrq, "POLL_INTERVAL_SECONDS", 0.0)
    log = tmp_path / "console.log"
    log.write_bytes(b"boot\n")
    monkeypatch.setattr(diagnostic_sysrq, "console_log_path", lambda _sid: log)
    control = _FakeControl(log, b"dump\n")

    async def _go() -> tuple[str | None, str | None, list[tuple[str, str, str]]]:
        async with _pool(migrated_url) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool, SystemState.READY)
            job = _job(system_id, "show_memory")  # same job replayed
            store = _FakeStore()
            first = await _run(pool, store, control, job)
            second = await _run(pool, store, control, job)
            rows = await _artifact_rows(pool, system_id)
            return first, second, rows

    first, second, rows = asyncio.run(_go())

    assert first == second  # stable result_ref across the retry
    assert len(rows) == 1  # insert-if-absent: no duplicate row


def test_system_not_ready_fails_system_changed_state(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostic_sysrq, "POLL_INTERVAL_SECONDS", 0.0)
    log = tmp_path / "console.log"
    log.write_bytes(b"boot\n")
    monkeypatch.setattr(diagnostic_sysrq, "console_log_path", lambda _sid: log)
    control = _FakeControl(log, b"dump\n")

    async def _go() -> None:
        async with _pool(migrated_url) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool, SystemState.PROVISIONING)
            await _run(pool, _FakeStore(), control, _job(system_id, "show_memory"))

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(_go())
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "system_changed_state"


def test_missing_object_store_fails_configuration_error(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostic_sysrq, "POLL_INTERVAL_SECONDS", 0.0)
    log = tmp_path / "console.log"
    log.write_bytes(b"boot\n")
    monkeypatch.setattr(diagnostic_sysrq, "console_log_path", lambda _sid: log)
    control = _FakeControl(log, b"dump\n")

    async def _go() -> None:
        async with _pool(migrated_url) as pool:
            await pool.open()
            system_id = await _seed_ready_system(pool, SystemState.READY)
            await _run(pool, None, control, _job(system_id, "show_memory"))

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(_go())
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "object_store_unavailable"
