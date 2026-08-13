"""Worker handler for supervised ``capture_traffic`` jobs (ADR-0385/0432/0558).

The handler freezes provider/publication facts under the Run lock, validates and later trims an
optional BPF filter, delegates provider mutation to the durable capture-operation supervisor, and
keeps the existing Run-owned SENSITIVE artifact publication path unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.discard import discard_unregistered_objects
from kdive.artifacts.etag_repair import reconcile_row_etag
from kdive.artifacts.pcap_count import count_pcap_packets
from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact, artifact_key
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import (
    ALLOCATIONS,
    ARTIFACTS,
    JOBS,
    RESOURCES,
    RUNS,
    SYSTEMS,
    ArtifactClaimConflict,
)
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.jobs.capture_operations.supervisor import (
    CaptureOperationSupervisor,
    CaptureSnapshot,
    require_capture_authority,
)
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import CaptureTrafficPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security import audit
from kdive.security.artifacts.bpf_filter import trim_pcap, validate_bpf
from kdive.store.objectstore import ObjectStore

_log = logging.getLogger(__name__)

_TENANT = "local"
_OWNER_KIND = "runs"
_RETENTION_CLASS = "pcap"

# Empty pcap = the 24-byte libpcap global header, no records. The agent detects this from
# artifacts.fetch_raw's size_bytes; the handler uses it only for a telemetry log line.
_PCAP_HEADER_LEN = 24

POLL_INTERVAL_SECONDS = 0.5

_ARTIFACT_ROW_SQL: LiteralString = (
    "SELECT id, etag FROM artifacts WHERE owner_kind = 'runs' AND owner_id = %s AND object_key = %s"
)


@dataclass(frozen=True, slots=True)
class LoopResult:
    """Why the capture size-poll ended."""

    truncated: bool  # stopped because the file reached max_bytes
    canceled: bool  # stopped because the owning job was canceled


async def run_capture_loop(
    *,
    stat: Callable[[], Awaitable[int]],
    sleep: Callable[[float], Awaitable[object]],
    canceled: Callable[[], Awaitable[bool]],
    max_bytes: int,
    max_polls: int,
) -> LoopResult:
    """Poll the growing pcap until the window elapses, it hits ``max_bytes``, or the job cancels.

    ``stat``/``sleep``/``canceled`` are injected async callables so the loop is libvirt-free and
    unit-testable. Bounded by ``max_polls`` (= the window in poll intervals); the caller detaches
    the filter on every exit path.
    """
    for _ in range(max_polls):
        await sleep(POLL_INTERVAL_SECONDS)
        if await canceled():
            return LoopResult(truncated=False, canceled=True)
        if await stat() >= max_bytes:
            return LoopResult(truncated=True, canceled=False)
    return LoopResult(truncated=False, canceled=False)


def _changed_state_error(run_id: UUID) -> CategorizedError:
    return CategorizedError(
        "run's system left the ready local-libvirt state during traffic capture",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": "system_changed_state", "run_id": str(run_id)},
    )


async def _snapshot(
    conn: AsyncConnection, run_id: UUID, resolver: ProviderResolver
) -> CaptureSnapshot:
    """Under the per-Run lock (tx 1): verify Run→System is READY+local and resolve the capturer."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        run = await RUNS.get(conn, run_id)
        if run is None or run.system_id is None:
            raise _changed_state_error(run_id)
        system = await SYSTEMS.get(conn, run.system_id)
        if system is None or system.state is not SystemState.READY:
            raise _changed_state_error(run_id)
        allocation = await ALLOCATIONS.get(conn, system.allocation_id)
        if allocation is None or allocation.resource_id is None:
            raise _changed_state_error(run_id)
        resource = await RESOURCES.get(conn, allocation.resource_id)
        if resource is None:
            raise _changed_state_error(run_id)
        binding = await resolver.binding_for_system(conn, system.id)
        set_provider_kind(binding.kind.value)
        # No identity gate here: the tool layer already refuses a provider without the
        # ``supports_traffic_capture`` capability (registrar.py), and this port-presence check is
        # the defence-in-depth backstop — so a second provider that wires a ``TrafficCapturer`` is
        # reachable the moment it is composed, with no gate change (ADR-0427).
        capturer = binding.runtime.traffic_capturer
        if capturer is None:
            raise CategorizedError(
                "provider does not support traffic capture",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": "traffic_capture_unsupported"},
            )
        operation_ports = binding.runtime.traffic_capture_operation
        if operation_ports is None or binding.kind.value not in {
            "local-libvirt",
            "remote-libvirt",
        }:
            raise CategorizedError(
                "provider does not support supervised traffic capture",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": "traffic_capture_supervision_unsupported"},
            )
        return CaptureSnapshot(
            provider_kind=binding.kind.value,
            resource_id=resource.id,
            system_id=system.id,
            domain_name=system.domain_name or domain_name_for(system.id),
            project=run.project,
            write_remediation=capturer.write_remediation,
            configuration=lambda: operation_ports.configuration(resource.id),
            quiescence=operation_ports.quiescence,
        )


async def _job_canceled(conn: AsyncConnection, job_id: UUID) -> bool:
    row = await JOBS.get(conn, job_id)
    return row is not None and row.state is JobState.CANCELED


class _ExistingRow(NamedTuple):
    """A committed pcap row for this object key: its id, and the object etag it describes."""

    id: UUID
    etag: str


async def _existing_artifact_row(
    conn: AsyncConnection, run_id: UUID, object_key: str
) -> _ExistingRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_ARTIFACT_ROW_SQL, (run_id, object_key))
        row = await cur.fetchone()
    return None if row is None else _ExistingRow(row["id"], str(row["etag"]))


async def _key_unregistered(conn: AsyncConnection, run_id: UUID, object_key: str) -> bool:
    """Whether no committed row claims ``object_key`` — the discard's row fence, run unlocked."""
    return await _existing_artifact_row(conn, run_id, object_key) is None


def _put_artifact(store: ObjectStore, run_id: UUID, name: str, data: bytes) -> StoredArtifact:
    return store.put_artifact(
        ArtifactWriteRequest(
            tenant=_TENANT,
            owner_kind=_OWNER_KIND,
            owner_id=str(run_id),
            name=name,
            data=data,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class=_RETENTION_CLASS,
        )
    )


async def _finish_task_after_cancellation[T](task: asyncio.Task[T]) -> T:
    """Drain an owned task without propagating caller cancellation into it."""
    current = asyncio.current_task()
    assert current is not None
    completed = asyncio.Event()
    task.add_done_callback(lambda _task: completed.set())
    while not completed.is_set():
        try:
            await completed.wait()
        except asyncio.CancelledError:
            current.uncancel()
    return task.result()


async def _discard_canceled_put(
    conn: AsyncConnection,
    store: ObjectStore,
    run_id: UUID,
    job_id: UUID,
    put_task: asyncio.Task[StoredArtifact],
) -> None:
    try:
        stored = await _finish_task_after_cancellation(put_task)
    except Exception as error:
        _log.warning(
            "canceled capture PUT for job %s failed before cleanup (%s)",
            job_id,
            type(error).__name__,
        )
        return
    discard_task = asyncio.create_task(
        discard_unregistered_objects(
            store,
            [stored],
            still_unregistered=lambda key: _key_unregistered(conn, run_id, key),
        )
    )
    try:
        await _finish_task_after_cancellation(discard_task)
    except Exception as error:
        _log.warning(
            "canceled capture PUT cleanup for job %s failed (%s)",
            job_id,
            type(error).__name__,
        )


async def _store_capture(
    conn: AsyncConnection, store: ObjectStore, job: Job, run_id: UUID, project: str, data: bytes
) -> UUID | None:
    """Store the pcap and register its row; ``None`` if the job was canceled.

    Three phases, so the per-Run advisory lock never spans the object-store PUT (ADR-0519):
    a locked guard read (tx 2), the unlocked PUT, then a locked re-read plus the row insert
    and audit (tx 3). Both locked phases are short and purely database work, so a slow or
    retrying object store no longer bounds how long this Run is serialized.

    Insert-if-absent on the object key keeps an at-least-once retry from duplicating the row.
    The first phase's probe short-circuits the *sequential* retry before it writes anything, so
    the common case never overwrites the stored object out from under a committed row. It does
    not close the concurrent case: two attempts of one job can both pass phase 1 and both PUT
    (the lease can lapse mid-job, ``jobs/worker.py``), and whichever PUT lands last leaves the
    other's row describing bytes the object no longer holds. When this attempt wrote and then
    found a peer's row, :func:`~kdive.artifacts.etag_repair.reconcile_row_etag` stats the object
    and re-points the row at what it actually holds — stats it rather than assuming this
    attempt's own etag, because landing last in the store and last at the lock are independent.
    """
    name = f"pcap-{job.id}"
    object_key = artifact_key(_TENANT, _OWNER_KIND, str(run_id), name)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        if await _job_canceled(conn, job.id):
            return None
        existing = await _existing_artifact_row(conn, run_id, object_key)
    if existing is not None:
        return existing.id

    put_task = asyncio.create_task(asyncio.to_thread(_put_artifact, store, run_id, name, data))
    put_completed = asyncio.Event()
    put_task.add_done_callback(lambda _task: put_completed.set())
    try:
        await put_completed.wait()
    except asyncio.CancelledError as cancellation:
        current = asyncio.current_task()
        assert current is not None
        current.uncancel()
        await _discard_canceled_put(conn, store, run_id, job.id, put_task)
        raise cancellation
    stored = put_task.result()

    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
            canceled = await _job_canceled(conn, job.id)
            existing = await _existing_artifact_row(conn, run_id, object_key)
            if existing is None and not canceled:
                artifact = register_artifact_row(
                    stored, owner_kind=_OWNER_KIND, owner_id=run_id, run_id=run_id
                )
                claimed, inserted = await ARTIFACTS.claim(conn, artifact)
                if inserted:
                    await audit.record(
                        conn,
                        job_context_from_job(job, project),
                        audit.AuditEvent(
                            tool="control.capture_traffic",
                            object_kind="runs",
                            object_id=run_id,
                            transition="capture_traffic",
                            args={"run_id": str(run_id)},
                            project=project,
                        ),
                    )
                    return claimed.id
                existing = _ExistingRow(claimed.id, claimed.etag)
    except ArtifactClaimConflict:
        await discard_unregistered_objects(
            store,
            [stored],
            still_unregistered=lambda key: _key_unregistered(conn, run_id, key),
        )
        raise
    if existing is not None:
        # A peer attempt registered the key while this PUT was in flight, so one of the two PUTs
        # overwrote the object that row describes. Which one landed last is not knowable here —
        # this attempt may have been first to write and last to take the lock — so the repair
        # stats the object rather than assuming this attempt's etag. Outside the lock: it is
        # store I/O.
        await reconcile_row_etag(
            conn, store, row_id=existing.id, object_key=object_key, row_etag=existing.etag
        )
        return None if canceled else existing.id
    # The cancel landed while the object was in flight. Reclaim is row-driven, so the object
    # would be a permanent orphan — delete it. ``JobState.CANCELED`` is terminal (state.py's
    # transition table gives it no successors), so unlike the SystemState guards this one cannot
    # refuse here and admit a peer's registration afterwards; the fences in the discard are
    # defence in depth rather than the thing that makes this site safe.
    await discard_unregistered_objects(
        store,
        [stored],
        still_unregistered=lambda key: _key_unregistered(conn, run_id, key),
    )
    return None


def _unlink_quietly(path: Path) -> None:
    """Best-effort delete of a worker temp file; never masks the handler's real result or error."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _trim_on_worker(data: bytes, capture_filter: str) -> bytes:
    """Apply the BPF ``capture_filter`` to fetched pcap bytes, entirely worker-locally.

    The capture bytes may originate on a remote host (ADR-0432), so trimming operates on the
    fetched bytes via two worker temp files (``tcpdump -r/-w``) rather than the provider dest. The
    temps are always reclaimed.
    """
    workdir = Path(tempfile.mkdtemp(prefix="kdive-pcap-trim-"))
    raw = workdir / "raw.pcap"
    trimmed = workdir / "filtered.pcap"
    try:
        raw.write_bytes(data)
        trim_pcap(raw, trimmed, capture_filter)
        return trimmed.read_bytes()
    finally:
        _unlink_quietly(raw)
        _unlink_quietly(trimmed)
        with contextlib.suppress(OSError):
            workdir.rmdir()


async def capture_traffic_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    artifact_store: ObjectStore,
    supervisor: CaptureOperationSupervisor,
) -> str | None:
    """Capture host-side guest traffic into a Run-owned pcap; return its artifact id.

    A cancel observed during the poll (or before the store commits) writes nothing and returns
    ``None`` (the job ends canceled). A zero-packet capture is a success — the stored object is the
    bare libpcap header; the empty signal reaches the agent via ``artifacts.fetch_raw``'s size.
    """
    payload = load_payload(job, CaptureTrafficPayload)
    run_id = UUID(payload.run_id)
    snapshot = await _snapshot(conn, run_id, resolver)

    # Validate the BPF filter BEFORE the capture window: a filter tcpdump rejects raises a terminal
    # CONFIGURATION_ERROR, so the job dead-letters on the first attempt without wasting a capture
    # window, attaching a filter to the guest, or writing a host file to reclaim.
    if payload.capture_filter:
        await asyncio.to_thread(validate_bpf, payload.capture_filter)

    request = CaptureRequest(
        job_id=job.id,
        provider_kind=snapshot.provider_kind,
        resource_id=snapshot.resource_id,
        system_id=snapshot.system_id,
        domain_name=snapshot.domain_name,
        snaplen=payload.snaplen,
        max_bytes=payload.max_bytes,
        max_polls=max(1, math.ceil(payload.duration_s / POLL_INTERVAL_SECONDS)),
    )
    data = await supervisor.execute(conn, job, snapshot, request)
    if data is None:
        return None
    await require_capture_authority()
    if len(data) < _PCAP_HEADER_LEN:
        raise CategorizedError(
            "traffic capture produced no readable pcap",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "reason": "pcap_not_written",
                "bytes": len(data),
                "remediation": snapshot.write_remediation,
            },
        )
    if payload.capture_filter:
        data = await asyncio.to_thread(_trim_on_worker, data, payload.capture_filter)
    packets = count_pcap_packets(data)
    _log.info(
        "capture_traffic job %s: %d bytes, %d packets, filtered=%s",
        job.id,
        len(data),
        packets,
        bool(payload.capture_filter),
    )
    if len(data) <= _PCAP_HEADER_LEN:
        _log.info("capture_traffic job %s captured no packets (header-only pcap)", job.id)
    artifact_id = await _store_capture(conn, artifact_store, job, run_id, snapshot.project, data)
    return None if artifact_id is None else str(artifact_id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    artifact_store: ObjectStore,
    supervisor: CaptureOperationSupervisor,
) -> None:
    """Bind the ``capture_traffic`` job handler with its provider + store deps (no redaction)."""
    registry.register(
        JobKind.CAPTURE_TRAFFIC,
        lambda conn, job: capture_traffic_handler(
            conn,
            job,
            resolver=resolver,
            artifact_store=artifact_store,
            supervisor=supervisor,
        ),
    )
