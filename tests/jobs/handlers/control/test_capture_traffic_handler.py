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
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import (
    ArtifactWriteRequest,
    FetchedArtifact,
    HeadResult,
    StoredArtifact,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ArtifactClaimConflict
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.control import capture_traffic
from kdive.jobs.provider_context import clear_provider_kind, take_provider_kind
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.audit import args_digest
from kdive.store.objectstore import ObjectStore
from tests.clock import STORE_MTIME
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
        return StoredArtifact(
            request.key(),
            etag,
            request.sensitivity,
            request.retention_class,
            version_id="test-version",
        )

    def get_artifact(self, key: str, _etag: str | None) -> FetchedArtifact:
        data, sensitivity, retention = self.objects[key]
        return FetchedArtifact(data, sensitivity, retention)

    def head(self, key: str) -> HeadResult | None:
        """Serve the etag ``put_artifact`` derived, so ADR-0519's delete fence is testable."""
        if key not in self.objects:
            return None
        data, sensitivity, _retention = self.objects[key]
        return HeadResult(
            size_bytes=len(data),
            checksum_sha256=None,
            etag=hashlib.sha256(data).hexdigest(),
            sensitivity=sensitivity,
            last_modified=STORE_MTIME,
            version_id="test-version",
        )


class _FakeCapturer:
    """Full TrafficCapturer lifecycle over a worker temp dir; records every argument.

    ``prepare`` returns a per-job path under ``tmp_path``, ``attach`` writes ``pcap`` there, and
    ``fetch``/``captured_size``/``reclaim`` operate on that path — so the handler drives the same
    provider-dispatched file side both providers use, with no monkeypatching of module helpers.

    The base directory is the caller's ``tmp_path`` rather than a self-minted ``mkdtemp``: this is
    a plain helper class, not a fixture, so nothing would ever tear a self-minted directory down
    and every instantiation would leak one ``/tmp`` entry permanently (#1613).
    """

    def __init__(self, tmp_path: Path, pcap: bytes | None = _PCAP_ONE) -> None:
        self._pcap = pcap
        self._dir = tmp_path
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


def test_happy_path_pins_wiring_audit_and_return(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """A ready capture stores the pcap and pins every collaborator argument it passes through."""
    clear_provider_kind()
    store = _FakeStore()
    capturer = _FakeCapturer(tmp_path)

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


def test_stored_domain_falls_back_to_derived_name(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """A System without a stored domain name captures under the id-derived domain name."""
    store = _FakeStore()
    capturer = _FakeCapturer(tmp_path)

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


def test_nonexistent_run_is_configuration_error(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
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
                    _FakeCapturer(tmp_path),
                    job,
                    loop_result=capture_traffic.LoopResult(False, False),
                    monkeypatch=monkeypatch,
                )
            return excinfo.value

    err = asyncio.run(_go())
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.details == {"reason": "system_changed_state", "run_id": missing_run}


def test_non_ready_system_pins_changed_state_error(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """A System that left READY yields the changed-state error with its full message + details."""

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool, SystemState.CRASHED)
            with pytest.raises(CategorizedError) as excinfo:
                await _run(
                    pool,
                    _FakeStore(),
                    _FakeCapturer(tmp_path),
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
            resolver = provider_resolver(traffic_capturer=None, supports_traffic_capture=False)

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


def test_retry_is_idempotent(migrated_url: str, monkeypatch, tmp_path: Path) -> None:
    store = _FakeStore()
    capturer = _FakeCapturer(tmp_path)

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


def test_loopresult_cancel_stores_nothing(migrated_url: str, monkeypatch, tmp_path: Path) -> None:
    """A loop that reports canceled writes nothing and still detaches the filter."""
    store = _FakeStore()
    capturer = _FakeCapturer(tmp_path)

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


def test_canceled_job_before_store_writes_nothing(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """A job canceled in the DB is re-checked under the store lock: nothing is written.

    The loop is stubbed to report *not* canceled so the handler reaches ``_store_capture``,
    whose own cancel re-check (reading the real CANCELED job row) must still abort the write.
    """
    store = _FakeStore()
    capturer = _FakeCapturer(tmp_path)

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


def test_invalid_filter_fails_before_capture(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    # An invalid BPF filter is validated before attach: no capture runs, nothing is stored, and the
    # error is terminal (dead-letter, not retry). Requires tcpdump for the real validate_bpf.
    if shutil.which("tcpdump") is None:
        pytest.skip("tcpdump not installed")
    store = _FakeStore()
    capturer = _FakeCapturer(tmp_path)

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


def test_zero_packet_capture_is_success(migrated_url: str, monkeypatch, tmp_path: Path) -> None:
    store = _FakeStore()
    capturer = _FakeCapturer(tmp_path, pcap=_PCAP_HEADER)  # header-only = zero packets

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


def test_unwritten_pcap_pins_configuration_error(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    # The hypervisor could not write the pcap (dir not QEMU-writable/labeled): the raw file is
    # absent, so read yields < 24 bytes. This is a loud config failure, not a silent 0-byte success.
    store = _FakeStore()
    capturer = _FakeCapturer(tmp_path, pcap=None)  # attach writes nothing → dest never created

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


def _advisory_locks_held_by(url: str, backend_pid: int) -> int:
    """Count advisory locks ``backend_pid`` holds, probed from a **second**, sync connection.

    Synchronous because it is called from inside ``put_artifact``, which the handler runs on a
    worker thread via ``asyncio.to_thread``; and from a second backend because probing on the
    connection under test would perturb the transaction being measured.
    """
    with psycopg.connect(url, autocommit=True) as probe, probe.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s",
            (backend_pid,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


async def _locks_visible_while_one_is_held(url: str) -> int:
    """Control for :func:`_advisory_locks_held_by`: what it reports while a lock IS held.

    Without this, a ``locks_at_put == [0]`` assertion would pass just as happily if the probe
    could never see an advisory lock at all.
    """
    async with (
        await psycopg.AsyncConnection.connect(url) as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.RUN, uuid4()),
    ):
        return await asyncio.to_thread(_advisory_locks_held_by, url, conn.info.backend_pid)


class _LockProbingStore(_FakeStore):
    """A store that records the handler's own advisory-lock count at the moment of each PUT."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url
        self.backend_pid: int | None = None
        self.locks_at_put: list[int] = []
        self.deleted: list[str] = []
        self.deleted_versions: list[tuple[str, str]] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        assert self.backend_pid is not None, "the test must publish the handler's backend pid"
        self.locks_at_put.append(_advisory_locks_held_by(self._url, self.backend_pid))
        return super().put_artifact(request)

    def delete_version(self, key: str, version_id: str) -> None:
        self.deleted.append(key)
        self.deleted_versions.append((key, version_id))
        self.objects.pop(key, None)


class _CancelingStore(_LockProbingStore):
    """Cancels the owning job from a second backend while the PUT is in flight."""

    def __init__(self, url: str, job_id: UUID) -> None:
        super().__init__(url)
        self._job_id = job_id

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        with psycopg.connect(self._url, autocommit=True) as canceler:
            canceler.execute("UPDATE jobs SET state = 'canceled' WHERE id = %s", (self._job_id,))
        return super().put_artifact(request)


async def _run_probing(pool, store, capturer, job, *, monkeypatch):
    """Drive the handler the way the worker dispatches it, exposing its backend pid to the store.

    ``set_autocommit(True)`` mirrors ``JobWorker._run_handler`` and is load-bearing, not
    incidental: on a pooled non-autocommit connection the handler's ``conn.transaction()`` blocks
    are savepoints inside one implicit transaction that the pool ends, so a
    ``pg_advisory_xact_lock`` would outlive every block regardless of where the PUT sits
    (ADR-0506/ADR-0516). Only under the worker's dispatch does releasing the lock mean anything.
    """
    resolver = provider_resolver(traffic_capturer=capturer)
    monkeypatch.setattr(
        capture_traffic,
        "run_capture_loop",
        _LoopSpy(capture_traffic.LoopResult(truncated=False, canceled=False)),
    )
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        store.backend_pid = conn.info.backend_pid
        try:
            return await capture_traffic.capture_traffic_handler(
                conn, job, resolver=resolver, artifact_store=cast(ObjectStore, store)
            )
        finally:
            await conn.set_autocommit(False)


def test_put_artifact_holds_no_advisory_lock(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """The pcap PUT runs with the per-Run lock released, so store latency never bounds it (#1725).

    The store probes ``pg_locks`` for the handler's own backend at the instant of the PUT. The
    control probe pins that the same query *does* report a lock when one is held, so the zero is
    a released lock rather than a blind probe.
    """
    store = _LockProbingStore(migrated_url)
    capturer = _FakeCapturer(tmp_path)

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            ref = await _run_probing(pool, store, capturer, job, monkeypatch=monkeypatch)
            return (
                ref,
                await _artifact_rows(pool, run_id),
                await _locks_visible_while_one_is_held(migrated_url),
            )

    ref, rows, control = asyncio.run(_go())
    assert control >= 1  # the probe can see an advisory lock when one is held
    assert store.locks_at_put == [0]  # ...and saw none held while the pcap was being written
    assert ref is not None  # the capture still completed and registered its row
    assert len(rows) == 1


def test_cancel_during_put_discards_the_unregistered_object(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """A cancel landing while the PUT is in flight registers no row and deletes the object.

    Reclaim of Run-owned evidence is row-driven, so an object with no ``artifacts`` row would be
    permanent. The handler deletes exactly the key it wrote.
    """
    capturer = _FakeCapturer(tmp_path)

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            store = _CancelingStore(migrated_url, job.id)
            ref = await _run_probing(pool, store, capturer, job, monkeypatch=monkeypatch)
            return ref, await _artifact_rows(pool, run_id), store, run_id, job

    ref, rows, store, run_id, job = asyncio.run(_go())
    expected_key = f"local/runs/{run_id}/pcap-{job.id}"
    assert store.objects == {}  # the object did not survive the aborted registration
    assert store.deleted == [expected_key]  # exactly the key this attempt wrote
    assert rows == []  # and no row was registered for the canceled job
    assert ref is None


def test_discard_failure_does_not_mask_the_cancel_outcome(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """A store fault on the compensating delete is swallowed: the job still ends canceled."""
    capturer = _FakeCapturer(tmp_path)

    class _UndeletableStore(_CancelingStore):
        def delete_version(self, key: str, version_id: str) -> None:
            self.deleted.append(key)
            self.deleted_versions.append((key, version_id))
            raise CategorizedError(
                "delete_object failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            store = _UndeletableStore(migrated_url, job.id)
            ref = await _run_probing(pool, store, capturer, job, monkeypatch=monkeypatch)
            return ref, await _artifact_rows(pool, run_id), store, capturer

    ref, rows, store, capturer = asyncio.run(_go())
    assert store.deleted  # the compensating delete was attempted
    assert ref is None  # ...and its failure did not become the handler's result
    assert rows == []
    assert capturer.reclaimed  # the host-side pcap is still reclaimed


def test_repeated_claim_disappearance_discards_then_retries(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """A typed claim race conditionally removes the post-PUT object before job retry."""
    store = _LockProbingStore(migrated_url)
    capturer = _FakeCapturer(tmp_path)

    async def _vanishing_claim(*args: object, **kwargs: object) -> None:
        raise ArtifactClaimConflict("winner disappeared twice")

    monkeypatch.setattr(capture_traffic.ARTIFACTS, "claim", _vanishing_claim)

    async def _go() -> tuple[str, Job]:
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            with pytest.raises(ArtifactClaimConflict, match="winner disappeared twice"):
                await _run_probing(pool, store, capturer, job, monkeypatch=monkeypatch)
            return run_id, job

    run_id, job = asyncio.run(_go())
    expected_key = f"local/runs/{run_id}/pcap-{job.id}"
    assert store.deleted == [expected_key]
    assert store.objects == {}


# --- Two concurrent attempts of one job (#1725 H2) -------------------------------------

_PCAP_TWO = _PCAP_HEADER + struct.pack("<IIII", 0, 0, 4, 4) + b"\xff\xff\xff\xff"


class _StallingStore(_FakeStore):
    """Parks the FIRST ``put_artifact`` until released, so a peer attempt can overtake it.

    This is the shape a lapsed lease produces: two attempts of one job in flight at once, which
    ``jobs/worker.py`` guards against but explicitly cannot rule out ("risks mid-job reclaim and
    double-run"). Both attempts pass their phase-1 probe, both PUT the same deterministic key,
    and the PUT that lands last decides what the object holds.
    """

    def __init__(self, *, park_after_write: bool = False) -> None:
        super().__init__()
        self.first_put_arrived = threading.Event()
        self.release_first_put = threading.Event()
        self._park_after_write = park_after_write
        self._stalled = False

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        park = not self._stalled
        if park:
            self._stalled = True
        if park and not self._park_after_write:
            self._arrive_and_wait()
        stored = super().put_artifact(request)
        if park and self._park_after_write:
            # Parking AFTER the write puts this attempt's bytes in the store FIRST while its
            # phase 3 runs LAST — the ordering that tells an assumed etag from an observed one.
            self._arrive_and_wait()
        return stored

    def _arrive_and_wait(self) -> None:
        self.first_put_arrived.set()
        self.release_first_put.wait(timeout=30)


async def _run_attempt(pool, store, capturer, job, *, monkeypatch=None):
    """One worker-shaped attempt, on its own connection (autocommit, as the worker dispatches)."""
    resolver = provider_resolver(traffic_capturer=capturer)
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            return await capture_traffic.capture_traffic_handler(
                conn, job, resolver=resolver, artifact_store=cast(ObjectStore, store)
            )
        finally:
            await conn.set_autocommit(False)


def test_concurrent_attempt_overwriting_the_object_repairs_the_rows_etag(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """When a peer registers the key and this attempt's PUT then lands, the row is repaired.

    Attempt A parks inside its PUT; B runs to completion and commits ``row_B`` describing B's
    bytes; A's PUT then overwrites the object with A's bytes. Phase 1's probe cannot prevent
    this — both attempts passed it before either wrote — so phase 3 refreshes ``row_B``'s etag
    to what the object actually holds. Without that repair the row describes bytes that are
    gone, which ``handlers/artifacts/vmcore.py`` treats as a job-failing corruption.
    """
    store = _StallingStore()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    capturer_a = _FakeCapturer(tmp_path / "a", pcap=_PCAP_ONE)
    capturer_b = _FakeCapturer(tmp_path / "b", pcap=_PCAP_TWO)
    monkeypatch.setattr(
        capture_traffic,
        "run_capture_loop",
        _LoopSpy(capture_traffic.LoopResult(truncated=False, canceled=False)),
    )

    async def _go():
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            task_a = asyncio.create_task(_run_attempt(pool, store, capturer_a, job))
            await asyncio.to_thread(store.first_put_arrived.wait, 30)
            ref_b = await _run_attempt(pool, store, capturer_b, job)
            store.release_first_put.set()
            ref_a = await task_a
            return ref_a, ref_b, await _artifact_rows(pool, run_id), run_id, job

    ref_a, ref_b, rows, run_id, job = asyncio.run(_go())
    key = f"local/runs/{run_id}/pcap-{job.id}"
    assert len(rows) == 1  # insert-if-absent held across the two concurrent attempts
    assert ref_a == ref_b  # both attempts report the one artifact
    # A's PUT landed last, so the object holds A's bytes...
    assert store.objects[key][0] == _PCAP_ONE

    # ...and the committed row was repaired to describe them rather than B's.
    async def _row_etag():
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn, conn.cursor() as c:
            await c.execute("SELECT etag FROM artifacts WHERE object_key = %s", (key,))
            row = await c.fetchone()
            assert row is not None
            return row[0]

    assert asyncio.run(_row_etag()) == hashlib.sha256(_PCAP_ONE).hexdigest()


def test_the_etag_repair_writes_the_observed_etag_not_this_attempts(
    migrated_url: str, monkeypatch, tmp_path: Path
) -> None:
    """The attempt that reaches phase 3 last must not stamp its own etag over a correct row.

    The unfavourable ordering, which the favourable-order test above structurally cannot reach:
    B's PUT lands FIRST, A then PUTs and registers ``row(etag_A)`` — correct, the object holds
    A's bytes — and only then does B reach its phase 3. B finds a row whose etag differs from
    what B wrote, and an etag repair that assumed B's own etag would replace a right answer with
    a wrong one. It must stat the object and leave ``etag_A`` in place.
    """
    store = _StallingStore(park_after_write=True)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    capturer_b = _FakeCapturer(tmp_path / "b", pcap=_PCAP_TWO)
    capturer_a = _FakeCapturer(tmp_path / "a", pcap=_PCAP_ONE)
    monkeypatch.setattr(
        capture_traffic,
        "run_capture_loop",
        _LoopSpy(capture_traffic.LoopResult(truncated=False, canceled=False)),
    )

    async def _go():
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4, open=False) as pool:
            await pool.open()
            run_id, _ = await _seed_ready_run(pool)
            job = _job(run_id)
            await _insert_job(pool, job, JobState.RUNNING)
            # B writes its bytes, then parks before its phase 3.
            task_b = asyncio.create_task(_run_attempt(pool, store, capturer_b, job))
            await asyncio.to_thread(store.first_put_arrived.wait, 30)
            # A now runs end to end: its PUT lands last, and it commits the row.
            ref_a = await _run_attempt(pool, store, capturer_a, job)
            store.release_first_put.set()
            ref_b = await task_b
            return ref_a, ref_b, await _artifact_rows(pool, run_id), run_id, job

    ref_a, ref_b, rows, run_id, job = asyncio.run(_go())
    key = f"local/runs/{run_id}/pcap-{job.id}"
    assert len(rows) == 1
    assert ref_a == ref_b
    assert store.objects[key][0] == _PCAP_ONE  # A's PUT landed last

    async def _row_etag():
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn, conn.cursor() as c:
            await c.execute("SELECT etag FROM artifacts WHERE object_key = %s", (key,))
            row = await c.fetchone()
            assert row is not None
            return row[0]

    # The row still describes the object. Stamping B's etag here would be the drift the repair
    # exists to remove, introduced by the repair itself.
    assert asyncio.run(_row_etag()) == hashlib.sha256(_PCAP_ONE).hexdigest()
