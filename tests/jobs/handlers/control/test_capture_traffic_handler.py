"""Tests for the capture_traffic worker job handler (ADR-0385, #1258).

Drives ``capture_traffic_handler`` directly with an in-memory object store and a migrated
Postgres connection. A fake TrafficCapturer writes a canned pcap to the dest path on ``attach``;
``run_capture_loop`` is stubbed so the flow (attach → loop → detach → store) is exercised without
real sleeps or a live guest. The stub records the keyword args the handler passes so the
collaborator wiring (stat / sleep / canceled / bounds) is pinned, and the fakes record the
domain / qom / snaplen / path arguments so the provider-attach + reclaim wiring is pinned too.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import struct
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import ArtifactWriteRequest, FetchedArtifact, StoredArtifact
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.control import capture_traffic
from kdive.jobs.provider_context import clear_provider_kind, take_provider_kind
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.audit import args_digest
from kdive.store.objectstore import ObjectStore
from tests.integration._seed import seed_granted_allocation, seed_running_run, seed_system
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 1, 1, tzinfo=UTC)

# A minimal valid 1-record little-endian pcap (24-byte header + 1 record of 4 payload bytes).
_PCAP_HEADER = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
_PCAP_ONE = _PCAP_HEADER + struct.pack("<IIII", 0, 0, 4, 4) + b"\x00\x00\x00\x00"


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, Sensitivity, str]] = {}

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.objects[request.key()] = (request.data, request.sensitivity, request.retention_class)
        etag = hashlib.sha256(request.data).hexdigest()
        return StoredArtifact(request.key(), etag, request.sensitivity, request.retention_class)

    def get_artifact(self, key: str, _etag: str | None) -> FetchedArtifact:
        data, sensitivity, retention = self.objects[key]
        return FetchedArtifact(data, sensitivity, retention)


class _FakeCapturer:
    """Full TrafficCapturer lifecycle over a worker temp dir; records every argument.

    ``prepare`` returns a per-job path under its own temp dir, ``attach`` writes ``pcap`` there, and
    ``fetch``/``captured_size``/``reclaim`` operate on that path — so the handler drives the same
    provider-dispatched file side both providers use, with no monkeypatching of module helpers.
    """

    def __init__(self, pcap: bytes | None = _PCAP_ONE) -> None:
        self._pcap = pcap
        self._dir = Path(tempfile.mkdtemp(prefix="kdive-pcap-test-"))
        self.prepared: list[tuple[UUID, UUID]] = []
        self.attached: list[dict[str, Any]] = []
        self.detached: list[dict[str, Any]] = []
        self.reclaimed: list[str] = []

    @property
    def write_remediation(self) -> str:
        return "fake remediation: make the capture destination writable"

    def prepare(self, system_id: UUID, job_id: UUID) -> str:
        self.prepared.append((system_id, job_id))
        return str(self._dir / f"{job_id}.pcap")

    def attach(self, domain_name, *, qom_id, dest_path, snaplen) -> None:
        self.attached.append(
            {"domain": domain_name, "qom_id": qom_id, "dest_path": dest_path, "snaplen": snaplen}
        )
        if self._pcap is not None:
            Path(dest_path).write_bytes(self._pcap)

    def detach(self, domain_name, *, qom_id) -> None:
        self.detached.append({"domain": domain_name, "qom_id": qom_id})

    def captured_size(self, dest_path: str) -> int:
        path = Path(dest_path)
        return path.stat().st_size if path.exists() else 0

    def fetch(self, dest_path: str, *, max_bytes: int) -> bytes:
        path = Path(dest_path)
        return path.read_bytes() if path.exists() else b""

    def reclaim(self, dest_path: str) -> None:
        self.reclaimed.append(dest_path)
        Path(dest_path).unlink(missing_ok=True)


def _job(run_id: str, *, capture_filter: str | None = None, duration_s: int = 1) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.CAPTURE_TRAFFIC,
        payload={
            "run_id": run_id,
            "duration_s": duration_s,
            "max_bytes": 67108864,
            "snaplen": 128,
            **({"capture_filter": capture_filter} if capture_filter else {}),
        },
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "user-1", "agent_session": None, "project": "proj"},
        dedup_key=f"{run_id}:capture_traffic",
    )


def _pool(url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(url, min_size=1, max_size=2, open=False)


async def _seed_ready_run(
    pool: AsyncConnectionPool,
    state: SystemState = SystemState.READY,
    *,
    domain_name: str | None = "kdive-x",
) -> tuple[str, str]:
    """Seed a ready local-libvirt run; return ``(run_id, system_id)``."""
    alloc_id = await seed_granted_allocation(pool, project="proj")
    sys_id = await seed_system(pool, alloc_id, state, project="proj", domain_name=domain_name)
    run_id = await seed_running_run(pool, sys_id)
    return run_id, sys_id


async def _insert_job(pool: AsyncConnectionPool, job: Job, state: JobState) -> None:
    """Persist ``job`` at ``state`` with its exact id so ``_job_canceled`` reads a real row."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, kind, payload, state, max_attempts, authorizing, dedup_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                job.id,
                job.kind.value,
                Jsonb(job.payload),
                state.value,
                job.max_attempts,
                Jsonb(job.authorizing),
                job.dedup_key,
            ),
        )


class _LoopSpy:
    """Records the kwargs ``run_capture_loop`` was called with and returns a fixed result.

    ``canceled_probe`` holds the result of invoking the recorded ``canceled`` callback *while
    the handler's connection is still open*, so a test can assert the callback reads the real
    job row without touching a closed connection.
    """

    def __init__(self, result: capture_traffic.LoopResult) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}
        self.canceled_probe: bool | None = None

    async def __call__(self, **kwargs: Any) -> capture_traffic.LoopResult:
        self.kwargs = kwargs
        self.canceled_probe = await kwargs["canceled"]()
        return self.result


async def _run_with_spy(pool, store, capturer, job, *, loop_spy, monkeypatch):
    """Drive the handler with a stubbed loop; the capturer owns its own dest path (no patching)."""
    resolver = provider_resolver(traffic_capturer=capturer)
    monkeypatch.setattr(capture_traffic, "run_capture_loop", loop_spy)
    async with pool.connection() as conn:
        return await capture_traffic.capture_traffic_handler(
            conn, job, resolver=resolver, artifact_store=cast(ObjectStore, store)
        )


async def _run(pool, store, capturer, job, *, loop_result, monkeypatch):
    spy = _LoopSpy(loop_result)
    return await _run_with_spy(pool, store, capturer, job, loop_spy=spy, monkeypatch=monkeypatch)


async def _artifact_rows(pool, run_id: str):
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, object_key, sensitivity, retention_class, run_id FROM artifacts "
            "WHERE owner_kind = 'runs' AND owner_id = %s",
            (UUID(run_id),),
        )
        return [tuple(r) for r in await cur.fetchall()]


async def _audit_rows(pool, run_id: str):
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT tool, object_kind, object_id, transition, args_digest, project "
            "FROM audit_log WHERE object_id = %s",
            (UUID(run_id),),
        )
        return await cur.fetchall()


def test_happy_path_pins_wiring_audit_and_return(migrated_url: str, monkeypatch) -> None:
    """A ready capture stores the pcap and pins every collaborator argument it passes through."""
    clear_provider_kind()
    store = _FakeStore()
    capturer = _FakeCapturer()

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, sys_id = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            spy = _LoopSpy(capture_traffic.LoopResult(truncated=False, canceled=False))
            ref = await _run_with_spy(
                pool, store, capturer, job, loop_spy=spy, monkeypatch=monkeypatch
            )
            kind = take_provider_kind()
            return {
                "ref": ref,
                "artifacts": await _artifact_rows(pool, run_id),
                "audit": await _audit_rows(pool, run_id),
                "capturer": capturer,
                "job": job,
                "run_id": run_id,
                "sys_id": sys_id,
                "provider_kind": kind,
                "loop_kwargs": spy.kwargs,
                "canceled_probe": spy.canceled_probe,
            }

    out = asyncio.run(_go())
    rows = out["artifacts"]
    assert len(rows) == 1
    row_id, object_key, sensitivity, retention, run_id_col = rows[0]
    # The returned ref is the stored artifact id, not a placeholder.
    assert out["ref"] is not None
    assert f"pcap-{out['job'].id}" in object_key
    assert sensitivity == "sensitive"
    assert retention == "pcap"
    # The pcap row carries the Run id, and the handler returns that same artifact id.
    assert str(run_id_col) == out["run_id"]
    assert out["ref"] == str(row_id)

    # Provider-kind tag is set for the worker's provider-op telemetry.
    assert out["provider_kind"] == "local-libvirt"

    # The provider prepares its own destination keyed on (system_id, job_id) — no worker-local
    # path assumption leaks into the handler.
    sys_id = out["sys_id"]
    job = out["job"]
    assert out["capturer"].prepared == [(UUID(sys_id), job.id)]
    # Attach/detach carry the resolved domain, the per-job qom id, the requested snaplen, dest.
    (attach,) = out["capturer"].attached
    assert attach["domain"] == "kdive-x"
    assert attach["qom_id"] == f"kdive-dump-{job.id}"
    assert attach["snaplen"] == 128
    assert attach["dest_path"].endswith(f"{job.id}.pcap")
    (detach,) = out["capturer"].detached
    assert detach["domain"] == "kdive-x"
    assert detach["qom_id"] == f"kdive-dump-{job.id}"
    # The host-side pcap is reclaimed on the success path.
    assert out["capturer"].reclaimed == [attach["dest_path"]]

    # The audit row records the exact capture_traffic transition tuple.
    audit = out["audit"]
    assert len(audit) == 1
    tool, object_kind, object_id, transition, digest, project = audit[0]
    assert tool == "control.capture_traffic"
    assert object_kind == "runs"
    assert str(object_id) == out["run_id"]
    assert transition == "capture_traffic"
    assert digest == args_digest({"run_id": out["run_id"]})
    assert project == "proj"

    # The loop is driven with the handler's real collaborators and computed bounds.
    kw = out["loop_kwargs"]
    for key in ("stat", "sleep", "canceled", "max_bytes", "max_polls"):
        assert key in kw
    assert kw["stat"] is not None
    assert kw["sleep"] is asyncio.sleep
    assert kw["canceled"] is not None
    assert kw["max_bytes"] == 67108864
    assert kw["max_polls"] == 2  # ceil(duration_s=1 / 0.5)
    assert out["canceled_probe"] is False  # the callback reads the real RUNNING job row


def test_stored_domain_falls_back_to_derived_name(migrated_url: str, monkeypatch) -> None:
    """A System without a stored domain name captures under the id-derived domain name."""
    store = _FakeStore()
    capturer = _FakeCapturer()

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, sys_id = await _seed_ready_run(pool, domain_name=None)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            await _run(
                pool,
                store,
                capturer,
                job,
                loop_result=capture_traffic.LoopResult(False, False),
                monkeypatch=monkeypatch,
            )
            return capturer, sys_id

    capturer, sys_id = asyncio.run(_go())
    (attach,) = capturer.attached
    assert attach["domain"] == domain_name_for(UUID(sys_id))


def test_nonexistent_run_is_configuration_error(migrated_url: str, monkeypatch) -> None:
    """A run_id with no Run row is a changed-state configuration error (not an AttributeError)."""
    store = _FakeStore()

    missing_run = str(uuid4())

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            job = _job(missing_run)  # run_id points at no Run row
            with pytest.raises(CategorizedError) as excinfo:
                await _run(
                    pool,
                    store,
                    _FakeCapturer(),
                    job,
                    loop_result=capture_traffic.LoopResult(False, False),
                    monkeypatch=monkeypatch,
                )
            return excinfo.value

    err = asyncio.run(_go())
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.details == {"reason": "system_changed_state", "run_id": missing_run}


def test_non_ready_system_pins_changed_state_error(migrated_url: str, monkeypatch) -> None:
    """A System that left READY yields the changed-state error with its full message + details."""

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool, SystemState.CRASHED)
            with pytest.raises(CategorizedError) as excinfo:
                await _run(
                    pool,
                    _FakeStore(),
                    _FakeCapturer(),
                    _job(run_id),
                    loop_result=capture_traffic.LoopResult(False, False),
                    monkeypatch=monkeypatch,
                )
            return excinfo.value, run_id

    err, run_id = asyncio.run(_go())
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(err) == "run's system left the ready local-libvirt state during traffic capture"
    assert err.details == {"reason": "system_changed_state", "run_id": run_id}


def test_unsupported_provider_pins_message(migrated_url: str, monkeypatch) -> None:
    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            resolver = provider_resolver(traffic_capturer=None)

            async def _fake_loop(**_kwargs):
                return capture_traffic.LoopResult(False, False)

            monkeypatch.setattr(capture_traffic, "run_capture_loop", _fake_loop)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as excinfo:
                    await capture_traffic.capture_traffic_handler(
                        conn,
                        _job(run_id),
                        resolver=resolver,
                        artifact_store=cast(ObjectStore, _FakeStore()),
                    )
                return excinfo.value

    err = asyncio.run(_go())
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.details["reason"] == "traffic_capture_unsupported"
    assert str(err) == "provider does not support traffic capture"


def test_retry_is_idempotent(migrated_url: str, monkeypatch) -> None:
    store = _FakeStore()
    capturer = _FakeCapturer()

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            first = await _run(
                pool,
                store,
                capturer,
                job,
                loop_result=capture_traffic.LoopResult(False, False),
                monkeypatch=monkeypatch,
            )
            second = await _run(
                pool,
                store,
                capturer,
                job,
                loop_result=capture_traffic.LoopResult(False, False),
                monkeypatch=monkeypatch,
            )
            return first, second, await _artifact_rows(pool, run_id)

    first, second, rows = asyncio.run(_go())
    assert first == second
    assert first is not None
    assert len(rows) == 1


def test_loopresult_cancel_stores_nothing(migrated_url: str, monkeypatch) -> None:
    """A loop that reports canceled writes nothing and still detaches the filter."""
    store = _FakeStore()
    capturer = _FakeCapturer()

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            ref = await _run(
                pool,
                store,
                capturer,
                job,
                loop_result=capture_traffic.LoopResult(truncated=False, canceled=True),
                monkeypatch=monkeypatch,
            )
            return ref, await _artifact_rows(pool, run_id), capturer

    ref, rows, capturer = asyncio.run(_go())
    assert ref is None
    assert rows == []
    assert capturer.detached  # detach still ran


def test_canceled_job_before_store_writes_nothing(migrated_url: str, monkeypatch) -> None:
    """A job canceled in the DB is re-checked under the store lock: nothing is written.

    The loop is stubbed to report *not* canceled so the handler reaches ``_store_capture``,
    whose own cancel re-check (reading the real CANCELED job row) must still abort the write.
    """
    store = _FakeStore()
    capturer = _FakeCapturer()

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.CANCELED)
            spy = _LoopSpy(capture_traffic.LoopResult(truncated=False, canceled=False))
            ref = await _run_with_spy(
                pool, store, capturer, job, loop_spy=spy, monkeypatch=monkeypatch
            )
            return ref, await _artifact_rows(pool, run_id), spy.canceled_probe

    ref, rows, canceled = asyncio.run(_go())
    assert ref is None  # store re-check aborted the write
    assert rows == []  # nothing persisted for a canceled job
    assert canceled is True  # the loop's canceled callback reads the real CANCELED row


def test_invalid_filter_fails_before_capture(migrated_url: str, monkeypatch) -> None:
    # An invalid BPF filter is validated before attach: no capture runs, nothing is stored, and the
    # error is terminal (dead-letter, not retry). Requires tcpdump for the real validate_bpf.
    if shutil.which("tcpdump") is None:
        pytest.skip("tcpdump not installed")
    store = _FakeStore()
    capturer = _FakeCapturer()

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            with pytest.raises(CategorizedError) as excinfo:
                await _run(
                    pool,
                    store,
                    capturer,
                    _job(run_id, capture_filter="this is not a filter )("),
                    loop_result=capture_traffic.LoopResult(False, False),
                    monkeypatch=monkeypatch,
                )
            return excinfo.value, await _artifact_rows(pool, run_id), capturer

    err, rows, capturer = asyncio.run(_go())
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.terminal is True
    assert rows == []
    assert capturer.attached == []  # validation failed before any filter-dump was attached


def test_zero_packet_capture_is_success(migrated_url: str, monkeypatch) -> None:
    store = _FakeStore()
    capturer = _FakeCapturer(pcap=_PCAP_HEADER)  # header-only = zero packets

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            ref = await _run(
                pool,
                store,
                capturer,
                _job(run_id),
                loop_result=capture_traffic.LoopResult(False, False),
                monkeypatch=monkeypatch,
            )
            return ref, await _artifact_rows(pool, run_id)

    ref, rows = asyncio.run(_go())
    assert ref is not None  # empty capture is a success
    assert len(rows) == 1


def test_unwritten_pcap_pins_configuration_error(migrated_url: str, monkeypatch) -> None:
    # The hypervisor could not write the pcap (dir not QEMU-writable/labeled): the raw file is
    # absent, so read yields < 24 bytes. This is a loud config failure, not a silent 0-byte success.
    store = _FakeStore()
    capturer = _FakeCapturer(pcap=None)  # attach writes nothing → dest never created

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            with pytest.raises(CategorizedError) as excinfo:
                await _run(
                    pool,
                    store,
                    capturer,
                    _job(run_id),
                    loop_result=capture_traffic.LoopResult(False, False),
                    monkeypatch=monkeypatch,
                )
            return excinfo.value, await _artifact_rows(pool, run_id), capturer

    err, rows, capturer = asyncio.run(_go())
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(err) == "traffic capture produced no readable pcap"
    assert err.details["reason"] == "pcap_not_written"
    assert err.details["bytes"] == 0
    assert "remediation" in err.details
    assert rows == []  # nothing stored
    assert capturer.detached  # detach still ran


def test_unlink_quietly_suppresses_oserror(tmp_path) -> None:
    """The reclaim helper swallows an OSError (a directory here) instead of masking the result."""
    # Unlinking a directory raises IsADirectoryError (an OSError); the helper must suppress it.
    capture_traffic._unlink_quietly(tmp_path)  # tmp_path is a directory, so unlink() raises
    assert tmp_path.exists()  # helper returned without raising and did not remove the directory
