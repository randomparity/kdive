"""Worker handler for supervised ``capture_traffic`` jobs (ADR-0385/0432/0558).

The handler freezes provider/publication facts under the Run lock, validates and later trims an
optional BPF filter, delegates provider mutation to the durable capture-operation supervisor, and
publishes the Run-owned SENSITIVE artifact under that operation's durable fence.
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
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.formats.pcap import count_pcap_packets
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import (
    ALLOCATIONS,
    JOBS,
    RESOURCES,
    RUNS,
    SYSTEMS,
)
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.jobs.capture_operations.storage.publication import CapturePublicationCoordinator
from kdive.jobs.capture_operations.storage.repository import CaptureOperation
from kdive.jobs.capture_operations.supervisor import (
    CaptureOperationSupervisor,
    CaptureSnapshot,
    require_capture_authority,
)
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import CaptureTrafficPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.artifacts.bpf_filter import trim_pcap, validate_bpf
from kdive.store.objectstore import ObjectStore

_log = logging.getLogger(__name__)

# Empty pcap = the 24-byte libpcap global header, no records. The agent detects this from
# artifacts.fetch_raw's size_bytes; the handler uses it only for a telemetry log line.
_PCAP_HEADER_LEN = 24

POLL_INTERVAL_SECONDS = 0.5


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
        "run's system left a ready state or supported traffic-capture provider",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": "system_changed_state", "run_id": str(run_id)},
    )


async def _snapshot(
    conn: AsyncConnection, run_id: UUID, resolver: ProviderResolver
) -> CaptureSnapshot:
    """Verify a ready Run→System uses a supported provider and resolve its capturer."""
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
    publication: CapturePublicationCoordinator,
    supervisor: CaptureOperationSupervisor,
) -> str | None:
    """Capture host-side guest traffic into a Run-owned pcap; return its artifact id.

    A cancel observed during the poll returns ``None`` (the job ends canceled). A zero-packet
    capture is a success — the stored object is the bare libpcap header; the empty signal reaches
    the agent via ``artifacts.fetch_raw``'s size.
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

    async def publish(
        conn: AsyncConnection,
        job: Job,
        operation: CaptureOperation,
        snapshot: CaptureSnapshot,
        data: bytes,
    ) -> UUID:
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
            _log.info(
                "capture_traffic job %s captured no packets (header-only pcap)",
                job.id,
            )
        return await publication.publish(
            conn,
            job,
            operation,
            snapshot,
            data,
        )

    artifact_id = await supervisor.execute(
        conn,
        job,
        snapshot,
        request,
        publisher=publish,
        publication_recoverer=publication.recover,
    )
    return None if artifact_id is None else str(artifact_id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    artifact_store: ObjectStore,
    supervisor: CaptureOperationSupervisor,
) -> None:
    """Bind the ``capture_traffic`` job handler with its provider + store deps (no redaction)."""
    publication = CapturePublicationCoordinator(artifact_store, supervisor.credential)
    registry.register(
        JobKind.CAPTURE_TRAFFIC,
        lambda conn, job: capture_traffic_handler(
            conn,
            job,
            resolver=resolver,
            publication=publication,
            supervisor=supervisor,
        ),
    )
