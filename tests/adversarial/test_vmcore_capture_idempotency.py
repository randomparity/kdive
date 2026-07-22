"""Adversarial: per-Run vmcore capture stays idempotent under concurrency (ADR-0244).

The capture handler's first-method-wins guard is enforced under a per-Run advisory lock
(``LockScope.RUN``) in both ``precheck_run`` (before the slow ``capture()`` seam) and
``finalize_capture`` (the race backstop). The falsifying case: two workers dispatch the same
Run+method capture concurrently, both pass ``precheck_run`` before either ``finalize_capture``
commits (a ``threading.Barrier`` in the capture seam forces both past precheck), so the *finalize*
re-check is the sole thing that prevents a second core row. These tests pin that exactly one
Run-owned raw core persists, and that two distinct Runs retain two distinct cores.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import StoredArtifact
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.artifacts import vmcore as vmcore_plane
from kdive.jobs.handlers.console.capture_telemetry import CaptureTelemetry
from kdive.jobs.payloads import Authorizing, CaptureVmcorePayload
from kdive.jobs.provider_context import take_provider_kind
from kdive.providers.ports.retrieve import CaptureOutput
from kdive.security.audit import args_digest
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.mcp.systems_support import provider_resolver

_AUTH = Authorizing(principal="alice", agent_session="s", project="proj")

_RAW_CORE_SQL = (
    "SELECT count(*) AS n FROM artifacts WHERE owner_kind = 'runs' AND owner_id = %s "
    "AND object_key LIKE '%%/vmcore-%%' AND object_key NOT LIKE '%%-redacted'"
)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=6, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _core(run_id: str, method: CaptureMethod = CaptureMethod.HOST_DUMP) -> CaptureOutput:
    raw = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{method.value}", "e1", Sensitivity.SENSITIVE, "vmcore"
    )
    red = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{method.value}-redacted", "e2", Sensitivity.REDACTED, "vmcore"
    )
    return CaptureOutput(raw=raw, redacted=red, vmcore_build_id="deadbeef", raw_size_bytes=512)


class _BarrierRetriever:
    """A retriever whose capture seam rendezvouses on a barrier, forcing the precheck race."""

    def __init__(self, run_id: str, barrier: threading.Barrier) -> None:
        self._run_id = run_id
        self._barrier = barrier
        self._lock = threading.Lock()
        self.calls = 0

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        with self._lock:
            self.calls += 1
        self._barrier.wait()  # both handlers reach here only after both passed precheck
        return _core(self._run_id, method)


async def _raw_core_count(pool: AsyncConnectionPool, run_id: str) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RAW_CORE_SQL, (run_id,))
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


def test_concurrent_same_run_capture_writes_one_core(migrated_url: str) -> None:
    # The barrier forces both handlers past precheck_run before either finalize commits, so the
    # finalize re-check (under LockScope.RUN) is the only thing preventing a second core. Exactly
    # one Run-owned raw core must persist, even though the capture seam ran twice.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.CAPTURE_VMCORE,
                    CaptureVmcorePayload(run_id=run_id, method=CaptureMethod.HOST_DUMP),
                    _AUTH,
                    f"{run_id}:capture_vmcore:host_dump",
                )
            barrier = threading.Barrier(2, timeout=15)
            retriever = _BarrierRetriever(run_id, barrier)
            resolver = provider_resolver(retriever=retriever)

            async def _handle() -> str | None:
                async with pool.connection() as conn:
                    return await vmcore_plane.capture_handler(conn, job, resolver=resolver)

            results = await asyncio.gather(_handle(), _handle())
            count = await _raw_core_count(pool, run_id)

        assert retriever.calls == 2, "both handlers must pass precheck and capture (the race)"
        assert count == 1, "the per-Run lock + finalize re-check must keep it to one core"
        assert results[0] == results[1], "both return the same Run-owned core key"

    asyncio.run(_run())


def test_two_runs_on_one_system_retain_distinct_cores(migrated_url: str) -> None:
    # Two distinct Runs each capturing a core retain two distinct Run-owned cores — the artifact
    # multiplicity ADR-0244 delivers (asserted at the ownership level; one system_id crashes at
    # most once today, so this seeds two Runs on the same crashed System directly).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_a = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            run_b = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            for run_id in (run_a, run_b):
                async with pool.connection() as conn:
                    job = await queue.enqueue(
                        conn,
                        JobKind.CAPTURE_VMCORE,
                        CaptureVmcorePayload(run_id=run_id, method=CaptureMethod.HOST_DUMP),
                        _AUTH,
                        f"{run_id}:capture_vmcore:host_dump",
                    )
                resolver = provider_resolver(retriever=_PlainRetriever(run_id))
                async with pool.connection() as conn:
                    await vmcore_plane.capture_handler(conn, job, resolver=resolver)
            count_a = await _raw_core_count(pool, run_a)
            count_b = await _raw_core_count(pool, run_b)

        assert count_a == 1 and count_b == 1, "each Run keeps its own core, neither shadows"
        assert run_a != run_b

    asyncio.run(_run())


class _PlainRetriever:
    """A retriever that returns a deterministic Run-owned core with no rendezvous."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        return _core(self._run_id, method)


# --- captured_method / method-suffix parsing (pure) ------------------------------------------


def test_captured_method_extracts_trailing_method() -> None:
    assert vmcore_plane.captured_method("local/runs/r/vmcore-host_dump") == "host_dump"


def test_captured_method_resolves_the_last_vmcore_segment() -> None:
    # rpartition, not partition: an earlier "/vmcore-" in the path must not shadow the trailing
    # method segment (a leading match would return the whole remainder, not the method).
    assert vmcore_plane.captured_method("a/vmcore-x/runs/r/vmcore-kdump") == "kdump"


def test_captured_method_rejects_empty_method_suffix() -> None:
    # Separator present but method empty must raise (the `or` guard), never return "".
    with pytest.raises(CategorizedError):
        vmcore_plane.captured_method("local/runs/r/vmcore-")


def test_captured_method_rejects_missing_suffix() -> None:
    with pytest.raises(CategorizedError):
        vmcore_plane.captured_method("local/runs/r/core.raw")


# --- handler collaborator args / audit / telemetry -------------------------------------------


class _RecordingRetriever:
    """A retriever recording the (system_id, run_id, method) it was driven with."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self.calls: list[tuple[UUID, UUID, CaptureMethod]] = []

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        self.calls.append((system_id, run_id, method))
        return _core(self._run_id, method)


async def _audit_rows(pool: AsyncConnectionPool, run_id: str) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT tool, object_kind, transition, args_digest FROM audit_log "
            "WHERE object_id = %s ORDER BY ts",
            (run_id,),
        )
        return list(await cur.fetchall())


async def _artifact_owner_kinds(pool: AsyncConnectionPool, run_id: str) -> list[str]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT owner_kind FROM artifacts WHERE owner_id = %s "
            "AND object_key LIKE '%%/vmcore-%%'",
            (run_id,),
        )
        return sorted(row[0] for row in await cur.fetchall())


def _duration_points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    assert data is not None
    out: list[Any] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    out.extend(metric.data.data_points)
    return out


def test_capture_handler_threads_args_records_audit_and_telemetry(migrated_url: str) -> None:
    async def _run() -> dict[str, Any]:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.CAPTURE_VMCORE,
                    CaptureVmcorePayload(run_id=run_id, method=CaptureMethod.HOST_DUMP),
                    _AUTH,
                    f"{run_id}:capture_vmcore:host_dump",
                )
            retriever = _RecordingRetriever(run_id)
            resolver = provider_resolver(retriever=retriever)
            reader = InMemoryMetricReader()
            meter = MeterProvider(metric_readers=[reader]).get_meter("t")
            telemetry = CaptureTelemetry(meter=meter)
            async with pool.connection() as conn:
                result = await vmcore_plane.capture_handler(
                    conn, job, resolver=resolver, telemetry=telemetry
                )
                # Same task as the handler: the provider-kind tag it set is still readable.
                provider_kind = take_provider_kind()
            return {
                "result": result,
                "calls": retriever.calls,
                "audit": await _audit_rows(pool, run_id),
                "owners": await _artifact_owner_kinds(pool, run_id),
                "reader": reader,
                "provider_kind": provider_kind,
                "sys_id": sys_id,
                "run_id": run_id,
            }

    out = asyncio.run(_run())
    run_id = out["run_id"]

    # The retriever is driven with the resolved (system.id, run.id, method) — none swapped to None.
    assert out["calls"] == [(UUID(out["sys_id"]), UUID(run_id), CaptureMethod.HOST_DUMP)]
    assert out["result"] == f"local/runs/{run_id}/vmcore-host_dump"
    # Both the raw and the redacted rows are Run-owned.
    assert out["owners"] == ["runs", "runs"]
    # Exactly one audit row with the exact tool/object/transition/args.
    assert out["audit"] == [
        {
            "tool": "vmcore.fetch",
            "object_kind": "runs",
            "transition": "capture_vmcore",
            "args_digest": args_digest({"run_id": run_id}),
        }
    ]
    # A success emits a duration point tagged ok + the method, and a byte-size point.
    reader = out["reader"]
    dur = _duration_points(reader, "kdive.vmcore.capture.duration")
    assert dur and dur[0].attributes["outcome"] == "ok"
    assert dur[0].attributes["capture_method"] == "host_dump"
    assert dur[0].sum < 60.0  # a real elapsed, not a boot-clock timestamp
    assert _duration_points(reader, "kdive.vmcore.capture.bytes"), "size_bytes recorded on success"
    # The handler tags the provider kind for metrics and stamps the same kind on the telemetry
    # point — both must be the real binding kind, neither swapped to None.
    provider_kind = out["provider_kind"]
    assert provider_kind is not None
    assert dur[0].attributes["provider"] == provider_kind


def test_capture_handler_missing_run_fails_closed(migrated_url: str) -> None:
    # A capture job whose Run does not exist must fail closed with a CONFIGURATION/INFRASTRUCTURE
    # CategorizedError under the lock (the `run is None or ...` guard), never an AttributeError
    # from dereferencing a None run.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            missing_run_id = str(UUID(int=0))
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.CAPTURE_VMCORE,
                    CaptureVmcorePayload(run_id=missing_run_id, method=CaptureMethod.HOST_DUMP),
                    _AUTH,
                    f"{missing_run_id}:capture_vmcore:host_dump",
                )
            resolver = provider_resolver(retriever=_PlainRetriever(missing_run_id))
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as excinfo:
                    await vmcore_plane.capture_handler(conn, job, resolver=resolver)
            assert excinfo.value.details["run_id"] == missing_run_id

    asyncio.run(_run())


def test_capture_handler_idempotent_recapture_returns_existing_core(migrated_url: str) -> None:
    # A second capture of the same Run+method returns the existing core without re-driving the
    # retriever — precheck_run must carry the real method (a None method would crash the re-check).
    async def _run() -> tuple[str | None, str | None, int, int, int]:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)

            async def _capture() -> tuple[str | None, int]:
                async with pool.connection() as conn:
                    job = await queue.enqueue(
                        conn,
                        JobKind.CAPTURE_VMCORE,
                        CaptureVmcorePayload(run_id=run_id, method=CaptureMethod.HOST_DUMP),
                        _AUTH,
                        f"{run_id}:capture_vmcore:host_dump",
                    )
                retriever = _RecordingRetriever(run_id)
                resolver = provider_resolver(retriever=retriever)
                async with pool.connection() as conn:
                    res = await vmcore_plane.capture_handler(conn, job, resolver=resolver)
                return res, len(retriever.calls)

            first, first_calls = await _capture()
            second, second_calls = await _capture()
            count = await _raw_core_count(pool, run_id)
            return first, second, first_calls, second_calls, count

    first, second, first_calls, second_calls, count = asyncio.run(_run())
    assert first == second, "the re-capture returns the same existing Run-owned core key"
    assert first_calls == 1, "the first capture drives the retriever"
    assert second_calls == 0, "the idempotent re-capture short-circuits before the retriever"
    assert count == 1, "no duplicate core row"
