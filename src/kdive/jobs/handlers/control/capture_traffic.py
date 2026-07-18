"""Worker handler for the `capture_traffic` job (ADR-0385, #1258).

Runs a QEMU filter-dump on a ready local-libvirt guest's SSH netdev for a bounded window and
stores the pcap as a Run-owned SENSITIVE artifact. The provider port is thin (attach/detach); this
handler owns the size-poll and the cooperative cancel-check (a plain async read of the job row on
the autocommit dispatch connection), so nothing crosses the synchronous libvirt thread boundary.
An optional BPF ``capture_filter`` is applied after capture with ``tcpdump -r/-w`` (validated by
``tcpdump -d``). The pcap is bounded by ``max_bytes``, so it is read whole and stored via
``put_artifact`` (that read also serves the ADR-0223 readback-wall check and the telemetry count).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.pcap_count import count_pcap_packets
from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact, artifact_key
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, JOBS, RUNS, SYSTEMS
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import CaptureTrafficPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.traffic import TrafficCapturer
from kdive.providers.shared.runtime_paths import (
    PCAP_HYPERVISOR_WRITE_REMEDIATION,
    domain_name_for,
    pcap_path,
    prepare_pcap_dir,
    read_pcap_bytes,
)
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
    "SELECT id FROM artifacts WHERE owner_kind = 'runs' AND owner_id = %s AND object_key = %s"
)


@dataclass(frozen=True, slots=True)
class LoopResult:
    """Why the capture size-poll ended."""

    truncated: bool  # stopped because the file reached max_bytes
    canceled: bool  # stopped because the owning job was canceled


async def run_capture_loop(*, stat, sleep, canceled, max_bytes: int, max_polls: int) -> LoopResult:
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


class _Snapshot(NamedTuple):
    system_id: UUID
    domain_name: str
    project: str
    capturer: TrafficCapturer


def _changed_state_error(run_id: UUID) -> CategorizedError:
    return CategorizedError(
        "run's system left the ready local-libvirt state during traffic capture",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": "system_changed_state", "run_id": str(run_id)},
    )


async def _snapshot(conn: AsyncConnection, run_id: UUID, resolver: ProviderResolver) -> _Snapshot:
    """Under the per-Run lock (tx 1): verify Run→System is READY+local and resolve the capturer."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        run = await RUNS.get(conn, run_id)
        if run is None or run.system_id is None:
            raise _changed_state_error(run_id)
        system = await SYSTEMS.get(conn, run.system_id)
        if system is None or system.state is not SystemState.READY:
            raise _changed_state_error(run_id)
        binding = await resolver.binding_for_system(conn, system.id)
        set_provider_kind(binding.kind.value)
        if binding.kind is not ResourceKind.LOCAL_LIBVIRT:
            raise CategorizedError(
                "traffic capture is supported only on local-libvirt Systems",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": "not_local_libvirt", "provider_kind": binding.kind.value},
            )
        capturer = binding.runtime.traffic_capturer
        if capturer is None:
            raise CategorizedError(
                "provider does not support traffic capture",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": "traffic_capture_unsupported"},
            )
        return _Snapshot(
            system_id=system.id,
            domain_name=system.domain_name or domain_name_for(system.id),
            project=run.project,
            capturer=capturer,
        )


async def _job_canceled(conn: AsyncConnection, job_id: UUID) -> bool:
    row = await JOBS.get(conn, job_id)
    return row is not None and row.state is JobState.CANCELED


async def _existing_artifact_id(
    conn: AsyncConnection, run_id: UUID, object_key: str
) -> UUID | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_ARTIFACT_ROW_SQL, (run_id, object_key))
        row = await cur.fetchone()
    return row["id"] if row is not None else None


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


async def _store_capture(
    conn: AsyncConnection, store: ObjectStore, job: Job, run_id: UUID, project: str, data: bytes
) -> UUID | None:
    """Under the per-Run lock (tx 2): re-check cancel, store the pcap, audit. ``None`` if canceled.

    Insert-if-absent on the object key keeps an at-least-once retry from duplicating the row.
    """
    name = f"pcap-{job.id}"
    object_key = artifact_key(_TENANT, _OWNER_KIND, str(run_id), name)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        if await _job_canceled(conn, job.id):
            return None
        existing = await _existing_artifact_id(conn, run_id, object_key)
        if existing is not None:
            return existing
        stored = await asyncio.to_thread(_put_artifact, store, run_id, name, data)
        artifact = register_artifact_row(
            stored, owner_kind=_OWNER_KIND, owner_id=run_id, run_id=run_id
        )
        await ARTIFACTS.insert(conn, artifact)
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
        return artifact.id


def _unlink_quietly(path: Path) -> None:
    """Best-effort delete of a host pcap file; never masks the handler's real result or error."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


async def capture_traffic_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    artifact_store: ObjectStore,
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

    qom_id = f"kdive-dump-{job.id}"
    dest = pcap_path(snapshot.system_id, job.id)
    trimmed = dest.with_suffix(".filtered.pcap")
    # Prepare the dir so the confined qemu:///system hypervisor can write the pcap (own to the
    # QEMU user + SELinux svirt_image_t); a bare mkdir leaves it unwritable by QEMU (ADR-0385).
    await asyncio.to_thread(prepare_pcap_dir, snapshot.system_id)

    max_polls = max(1, math.ceil(payload.duration_s / POLL_INTERVAL_SECONDS))

    async def _stat() -> int:
        return await asyncio.to_thread(lambda: dest.stat().st_size if dest.exists() else 0)

    await asyncio.to_thread(
        snapshot.capturer.attach,
        snapshot.domain_name,
        qom_id=qom_id,
        dest_path=str(dest),
        snaplen=payload.snaplen,
    )
    try:
        result = await run_capture_loop(
            stat=_stat,
            sleep=asyncio.sleep,
            canceled=lambda: _job_canceled(conn, job.id),
            max_bytes=payload.max_bytes,
            max_polls=max_polls,
        )
    finally:
        await asyncio.to_thread(snapshot.capturer.detach, snapshot.domain_name, qom_id=qom_id)

    if result.canceled:
        await asyncio.to_thread(_unlink_quietly, dest)
        return None

    # The host pcap files are always reclaimed — a read/trim/store failure must not leak them.
    try:
        # The whole read also performs the ADR-0223 readback-wall check (PermissionError -> error).
        data = await asyncio.to_thread(read_pcap_bytes, dest)
        # A successful filter-dump writes the 24-byte libpcap header immediately, so a missing or
        # short raw file means the hypervisor could not write it (dir not QEMU-writable/labeled) —
        # a config failure, NOT a valid zero-packet capture (which is exactly the 24-byte header).
        if len(data) < _PCAP_HEADER_LEN:
            raise CategorizedError(
                "traffic capture produced no readable pcap",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "reason": "pcap_not_written",
                    "bytes": len(data),
                    "remediation": PCAP_HYPERVISOR_WRITE_REMEDIATION,
                },
            )
        if payload.capture_filter:
            await asyncio.to_thread(trim_pcap, dest, trimmed, payload.capture_filter)
            data = await asyncio.to_thread(read_pcap_bytes, trimmed)

        packets = count_pcap_packets(data)
        _log.info(
            "capture_traffic job %s: %d bytes, %d packets, truncated=%s, filtered=%s",
            job.id,
            len(data),
            packets,
            result.truncated,
            bool(payload.capture_filter),
        )
        if len(data) <= _PCAP_HEADER_LEN:
            _log.info("capture_traffic job %s captured no packets (header-only pcap)", job.id)

        artifact_id = await _store_capture(
            conn, artifact_store, job, run_id, snapshot.project, data
        )
    finally:
        await asyncio.to_thread(_unlink_quietly, dest)
        await asyncio.to_thread(_unlink_quietly, trimmed)
    return None if artifact_id is None else str(artifact_id)


def register_handlers(
    registry: HandlerRegistry, *, resolver: ProviderResolver, artifact_store: ObjectStore
) -> None:
    """Bind the ``capture_traffic`` job handler with its provider + store deps (no redaction)."""
    registry.register(
        JobKind.CAPTURE_TRAFFIC,
        lambda conn, job: capture_traffic_handler(
            conn, job, resolver=resolver, artifact_store=artifact_store
        ),
    )
