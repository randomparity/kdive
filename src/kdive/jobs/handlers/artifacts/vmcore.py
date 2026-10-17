"""Worker handlers for the `vmcore.*` retrieve plane."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.catalog.read_model import redacted_vmcore_artifact_id
from kdive.artifacts.catalog.registration import register_artifact_row
from kdive.artifacts.storage import HeadResult
from kdive.artifacts.uploads.upload_manifest import RUN_UPLOAD_OWNER
from kdive.artifacts.uploads.write_lease import hold_write_lease, release_write_lease
from kdive.artifacts.vmcore_retention import (
    VmcoreExecution,
    journal_vmcore,
    publish_vmcore,
    published_vmcore,
    require_current_capture,
    require_vmcore_available,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS, SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run, System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.console.capture_telemetry import CaptureOutcome, CaptureTelemetry
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import CaptureVmcorePayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.retrieve import CaptureOutput
from kdive.providers.shared.staging.host_dump_volume_leases import (
    hold_host_dump_volume_lease,
    release_host_dump_volume_lease,
)
from kdive.security import audit

_DISABLED_TELEMETRY = CaptureTelemetry.disabled()


class CaptureObjectStore(Protocol):
    """The one store read this plane needs: stat a key it is about to reference (ADR-0497).

    Narrower than :class:`~kdive.store.objectstore.ObjectStore` on purpose. The verify needs the
    object's current identity and nothing else, and a port that cannot delete or write is a port
    that cannot be misused into compensating for a failed verify.
    """

    def head(self, key: str) -> HeadResult | None: ...


@dataclass(frozen=True, slots=True)
class ExistingCapture:
    """A prior same-method core for this Run, and the artifact reference that replays it.

    ``redacted_artifact_id`` is ``None`` when the raw core survives but its redacted sibling does
    not — a reclaimed or expired artifact row. The replay then publishes no result reference rather
    than a raw object key the caller cannot read: ``refs.result`` means "the redacted vmcore
    artifact id" for every ``capture_vmcore`` job or it means nothing (ADR-0466).
    """

    redacted_artifact_id: str | None


async def precheck_run(
    conn: AsyncConnection, run_id: UUID, method: CaptureMethod
) -> tuple[Run, System] | ExistingCapture:
    """Resolve a capture target or replay its published method under the Run lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        run = await RUNS.get(conn, run_id)
        if run is None or run.system_id is None:
            raise CategorizedError(
                "capture target run is gone or not bound to a system",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"run_id": str(run_id)},
            )
        await require_vmcore_available(conn, run_id)
        system = await SYSTEMS.get(conn, run.system_id)
        if system is None:
            raise CategorizedError(
                "capture target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(run.system_id)},
            )
        existing = await published_vmcore(conn, run_id)
        if existing is not None:
            if existing.method != method:
                raise CategorizedError(
                    "a vmcore captured via a different method already exists for this Run",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={
                        "run_id": str(run_id),
                        "existing_method": existing.method.value,
                        "requested_method": method.value,
                    },
                )
            return ExistingCapture(await redacted_vmcore_artifact_id(conn, run_id))
        return run, system


async def finalize_capture(
    conn: AsyncConnection,
    job: Job,
    run: Run,
    method: CaptureMethod,
    output: CaptureOutput,
    *,
    execution: VmcoreExecution,
    artifact_store: CaptureObjectStore,
) -> str | None:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        await require_vmcore_available(conn, run.id)
        await require_current_capture(conn, job.id, job.attempt)
        existing = await published_vmcore(conn, run.id)
        if existing is not None:
            if existing.method != method:
                raise CategorizedError(
                    "a vmcore captured via a different method already exists for this Run",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                )
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE vmcore_capture_executions SET state='retiring', retry_at=now() "
                    "WHERE id=%s AND state='capturing'",
                    (execution.id,),
                )
            await release_write_lease(conn, RUN_UPLOAD_OWNER, run.id, job.id)
            if run.system_id is not None:
                await release_host_dump_volume_lease(conn, run.system_id, job.id)
            return await redacted_vmcore_artifact_id(conn, run.id)
        if (
            output.raw.key != execution.raw_key
            or output.redacted.key != execution.redacted_key
            or output.raw.sensitivity != Sensitivity.SENSITIVE
            or output.redacted.sensitivity != Sensitivity.REDACTED
        ):
            raise CategorizedError(
                "provider returned inconsistent vmcore destinations or sensitivity",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        for stored in (output.raw, output.redacted):
            head = await asyncio.to_thread(artifact_store.head, stored.key)
            if (
                head is None
                or head.version_id != stored.version_id
                or head.etag != stored.etag
                or head.metadata.get("vmcore-execution") != str(execution.id)
                or head.metadata.get("vmcore-kind") != "capture"
                or head.sensitivity != stored.sensitivity
            ):
                raise CategorizedError(
                    "vmcore object identity changed before publication",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={
                        "object_key": stored.key,
                        "captured_etag": stored.etag,
                        "stored_etag": head.etag if head is not None else None,
                    },
                )
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.raw, owner_kind="runs", owner_id=run.id)
        )
        redacted = await ARTIFACTS.insert(
            conn, register_artifact_row(output.redacted, owner_kind="runs", owner_id=run.id)
        )
        await publish_vmcore(conn, execution, output.raw, output.redacted)
        await release_write_lease(conn, RUN_UPLOAD_OWNER, run.id, job.id)
        if run.system_id is not None:
            await release_host_dump_volume_lease(conn, run.system_id, job.id)
        await audit.record(
            conn,
            job_context_from_job(job, run.project),
            audit.AuditEvent(
                tool="vmcore.fetch",
                object_kind="runs",
                object_id=run.id,
                transition="capture_vmcore",
                args={"run_id": str(run.id)},
                project=run.project,
            ),
        )
    return str(redacted.id)


async def capture_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    artifact_store: CaptureObjectStore,
    telemetry: CaptureTelemetry = _DISABLED_TELEMETRY,
) -> str | None:
    """Capture the System's vmcore, store the raw + redacted rows, return the redacted id.

    The returned value becomes the job's ``result_ref`` and reaches the agent as ``refs.result``
    on every ``jobs.wait`` / ``jobs.list`` read — the redacted artifact id it hands
    straight to ``artifacts.get`` (ADR-0466). ``None`` when no redacted row survives.

    ``artifact_store`` is required rather than defaulted: it is what
    finalization stats through, and a ``None`` default that skipped the
    verify would be a data-loss guard that silently does nothing — the same failure mode ADR-0497
    §1 rejects the conditional delete for.

    A write lease is held over the Run's object prefix across the capture (ADR-0502), minted
    after ``precheck_run`` and released by :func:`finalize_capture`. The ``except`` below
    deliberately does **not** release it: a worker killed mid-write would release nothing anyway,
    so relying on an ``except`` here would be a fence that holds only for the failures Python got
    to observe.
    ``reap_stale_write_leases`` collects it instead, off the holding job's own liveness.

    A ``host_dump`` capture holds a second lease over its **System** across the same window
    (ADR-0562), fencing the reconciler's orphaned-volume sweep off the deterministic dump volume the
    provider is about to recreate. It follows the write lease's shape exactly — minted under the
    lock the sweep also takes, released by :func:`finalize_capture`, never by the ``except``
    — and is collected by ``reap_stale_host_dump_volume_leases``.
    """
    payload = load_payload(job, CaptureVmcorePayload)
    run_id = UUID(payload.run_id)
    method = payload.method
    precheck = await precheck_run(conn, run_id, method)
    if isinstance(precheck, ExistingCapture):
        return precheck.redacted_artifact_id
    run, system = precheck
    async with conn.transaction():
        binding = await resolver.binding_for_system(conn, system.id)
    tenant = {
        ResourceKind.LOCAL_LIBVIRT: "local",
        ResourceKind.REMOTE_LIBVIRT: "remote-libvirt",
        ResourceKind.FAULT_INJECT: "fault-inject",
    }[binding.kind]
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        execution = await journal_vmcore(
            conn,
            run_id=run.id,
            job_id=job.id,
            attempt=job.attempt,
            method=method,
            tenant=tenant,
        )
    # Journal and each lease commit before provider I/O; never co-hold RUN and SYSTEM.
    await hold_write_lease(conn, RUN_UPLOAD_OWNER, run.id, job.id)
    # Only host_dump creates the deterministic System dump volume (ADR-0562).
    # The prefix lease has committed, so this separate SYSTEM transaction cannot
    # become a savepoint that holds its lock across the provider operation.
    if method is CaptureMethod.HOST_DUMP:
        await hold_host_dump_volume_lease(conn, system.id, job.id)
    set_provider_kind(binding.kind.value)
    retriever = binding.runtime.retriever
    started = time.perf_counter()
    try:
        output = await asyncio.to_thread(
            retriever.capture, system.id, run.id, method, execution_id=execution.id
        )
        result = await finalize_capture(
            conn,
            job,
            run,
            method,
            output,
            execution=execution,
            artifact_store=artifact_store,
        )
    except Exception:
        elapsed = time.perf_counter() - started
        outcome: CaptureOutcome = "error"
        telemetry.record(method.value, binding.kind.value, outcome, seconds=elapsed)
        raise
    elapsed = time.perf_counter() - started
    outcome: CaptureOutcome = "ok"
    telemetry.record(
        method.value,
        binding.kind.value,
        outcome,
        seconds=elapsed,
        size_bytes=output.raw_size_bytes,
    )
    return result


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    artifact_store: CaptureObjectStore,
    telemetry: CaptureTelemetry = _DISABLED_TELEMETRY,
) -> None:
    """Bind the `capture_vmcore` job handler."""
    registry.register(
        JobKind.CAPTURE_VMCORE,
        lambda conn, job: capture_handler(
            conn, job, resolver=resolver, artifact_store=artifact_store, telemetry=telemetry
        ),
    )
