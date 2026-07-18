"""Tests for the capture_traffic worker job handler (ADR-0385, #1258).

Drives ``capture_traffic_handler`` directly with an in-memory object store and a migrated
Postgres connection. A fake TrafficCapturer writes a canned pcap to the dest path on ``attach``;
``run_capture_loop`` is stubbed so the flow (attach → loop → detach → store) is exercised without
real sleeps or a live guest.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import struct
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import ArtifactWriteRequest, FetchedArtifact, StoredArtifact
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.control import capture_traffic
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
    """Records attach/detach; ``attach`` writes ``pcap`` bytes to the dest path."""

    def __init__(self, pcap: bytes | None = _PCAP_ONE) -> None:
        self._pcap = pcap
        self.attached: list[str] = []
        self.detached: list[str] = []

    def attach(self, domain_name, *, qom_id, dest_path, snaplen) -> None:
        self.attached.append(qom_id)
        if self._pcap is not None:
            Path(dest_path).write_bytes(self._pcap)

    def detach(self, domain_name, *, qom_id) -> None:
        self.detached.append(qom_id)


def _job(run_id: str, *, capture_filter: str | None = None) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.CAPTURE_TRAFFIC,
        payload={
            "run_id": run_id,
            "duration_s": 1,
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


async def _seed_ready_run(pool: AsyncConnectionPool, state: SystemState = SystemState.READY) -> str:
    alloc_id = await seed_granted_allocation(pool, project="proj")
    sys_id = await seed_system(pool, alloc_id, state, project="proj", domain_name="kdive-x")
    return await seed_running_run(pool, sys_id)


async def _run(pool, store, capturer, job, *, loop_result, monkeypatch):
    resolver = provider_resolver(traffic_capturer=capturer)

    async def _fake_loop(**_kwargs):
        return loop_result

    base = Path(tempfile.mkdtemp(prefix="kdive-pcap-test-"))
    monkeypatch.setattr(capture_traffic, "run_capture_loop", _fake_loop)
    monkeypatch.setattr(capture_traffic, "prepare_pcap_dir", lambda _sid: base)
    monkeypatch.setattr(capture_traffic, "pcap_path", lambda _sid, jid: base / f"{jid}.pcap")
    async with pool.connection() as conn:
        return await capture_traffic.capture_traffic_handler(
            conn, job, resolver=resolver, artifact_store=cast(ObjectStore, store)
        )


async def _artifact_rows(pool, run_id: str):
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT object_key, sensitivity, retention_class FROM artifacts "
            "WHERE owner_kind = 'runs' AND owner_id = %s",
            (UUID(run_id),),
        )
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


def test_happy_path_stores_sensitive_pcap(migrated_url: str, monkeypatch) -> None:
    store = _FakeStore()
    capturer = _FakeCapturer()

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id = await _seed_ready_run(pool)
            job = _job(run_id)
            ref = await _run(
                pool,
                store,
                capturer,
                job,
                loop_result=capture_traffic.LoopResult(truncated=False, canceled=False),
                monkeypatch=monkeypatch,
            )
            return ref, await _artifact_rows(pool, run_id), capturer, job

    ref, rows, capturer, job = asyncio.run(_go())
    assert ref is not None
    assert len(rows) == 1
    object_key, sensitivity, retention = rows[0]
    assert f"pcap-{job.id}" in object_key
    assert sensitivity == "sensitive"
    assert retention == "pcap"
    assert capturer.attached == [f"kdive-dump-{job.id}"]
    assert capturer.detached == [f"kdive-dump-{job.id}"]


def test_non_ready_system_is_configuration_error(migrated_url: str, monkeypatch) -> None:
    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id = await _seed_ready_run(pool, SystemState.CRASHED)
            with pytest.raises(CategorizedError) as excinfo:
                await _run(
                    pool,
                    _FakeStore(),
                    _FakeCapturer(),
                    _job(run_id),
                    loop_result=capture_traffic.LoopResult(False, False),
                    monkeypatch=monkeypatch,
                )
            return excinfo.value

    err = asyncio.run(_go())
    assert err.category is ErrorCategory.CONFIGURATION_ERROR


def test_unsupported_provider_is_configuration_error(migrated_url: str, monkeypatch) -> None:
    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id = await _seed_ready_run(pool)
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


def test_retry_is_idempotent(migrated_url: str, monkeypatch) -> None:
    store = _FakeStore()
    capturer = _FakeCapturer()

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id = await _seed_ready_run(pool)
            job = _job(run_id)
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
    assert len(rows) == 1


def test_cancel_stores_nothing(migrated_url: str, monkeypatch) -> None:
    store = _FakeStore()
    capturer = _FakeCapturer()

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id = await _seed_ready_run(pool)
            job = _job(run_id)
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
            run_id = await _seed_ready_run(pool)
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
            run_id = await _seed_ready_run(pool)
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


def test_unwritten_pcap_is_configuration_error(migrated_url: str, monkeypatch) -> None:
    # The hypervisor could not write the pcap (dir not QEMU-writable/labeled): the raw file is
    # absent, so read yields < 24 bytes. This is a loud config failure, not a silent 0-byte success.
    store = _FakeStore()
    capturer = _FakeCapturer(pcap=None)  # attach writes nothing → dest never created

    async def _go():
        async with _pool(migrated_url) as pool:
            await pool.open()
            run_id = await _seed_ready_run(pool)
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
    assert err.details["reason"] == "pcap_not_written"
    assert "remediation" in err.details
    assert rows == []  # nothing stored
    assert capturer.detached  # detach still ran
