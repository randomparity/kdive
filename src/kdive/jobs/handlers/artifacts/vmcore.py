"""Worker handlers for the `vmcore.*` retrieve plane."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.read_model import raw_vmcore_key
from kdive.artifacts.registration import register_artifact_row
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS, SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run, System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.console.capture_telemetry import CaptureTelemetry
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import CaptureVmcorePayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.security import audit

_DISABLED_TELEMETRY = CaptureTelemetry.disabled()


def captured_method(object_key: str) -> str:
    """The method suffix of a raw vmcore key (`.../vmcore-host_dump` -> `host_dump`)."""
    _, sep, method = object_key.rpartition("/vmcore-")
    if not sep or not method:
        raise CategorizedError(
            "malformed raw vmcore object key (no method suffix)",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"object_key": object_key},
        )
    return method


def ensure_method_match(existing_key: str, method: CaptureMethod, run_id: UUID) -> None:
    """Raise `configuration_error` when an existing core used another capture method."""
    captured = captured_method(existing_key)
    if captured != method.value:
        raise CategorizedError(
            "a vmcore captured via a different method already exists for this Run",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "run_id": str(run_id),
                "existing_method": captured,
                "requested_method": method.value,
            },
        )


async def precheck_run(
    conn: AsyncConnection, run_id: UUID, method: CaptureMethod
) -> tuple[Run, System] | str:
    """Under the per-Run lock, return an existing same-method key, or the Run + bound System.

    Run-addressed (ADR-0244): the core is owned by the crashing Run, so the dedup guard and the
    advisory lock are scoped to ``run_id``. ``system`` is resolved from the Run's binding so the
    provider can locate the live domain/overlay/volume.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        run = await RUNS.get(conn, run_id)
        if run is None or run.system_id is None:
            raise CategorizedError(
                "capture target run is gone or not bound to a system",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"run_id": str(run_id)},
            )
        system = await SYSTEMS.get(conn, run.system_id)
        if system is None:
            raise CategorizedError(
                "capture target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(run.system_id)},
            )
        existing = await raw_vmcore_key(conn, run_id)
        if existing is not None:
            ensure_method_match(existing, method, run_id)
            return existing
        return run, system


async def finalize_capture(
    conn: AsyncConnection, job: Job, run: Run, method: CaptureMethod, output: Any
) -> str:
    """Insert both Run-owned artifact rows + audit under the per-Run lock (ADR-0244)."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        existing = await raw_vmcore_key(conn, run.id)
        if existing is not None:
            ensure_method_match(existing, method, run.id)
            return existing
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.raw, owner_kind="runs", owner_id=run.id)
        )
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.redacted, owner_kind="runs", owner_id=run.id)
        )
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
    return str(output.raw.key)


async def capture_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    telemetry: CaptureTelemetry = _DISABLED_TELEMETRY,
) -> str | None:
    """Capture the System's vmcore and store the raw + redacted rows."""
    payload = load_payload(job, CaptureVmcorePayload)
    run_id = UUID(payload.run_id)
    method = payload.method
    precheck = await precheck_run(conn, run_id, method)
    if isinstance(precheck, str):
        return precheck
    run, system = precheck
    binding = await resolver.binding_for_system(conn, system.id)
    set_provider_kind(binding.kind.value)
    retriever = binding.runtime.retriever
    started = time.perf_counter()
    try:
        output = await asyncio.to_thread(retriever.capture, system.id, run.id, method)
        result = await finalize_capture(conn, job, run, method, output)
    except Exception:
        elapsed = time.perf_counter() - started
        telemetry.record(method.value, binding.kind.value, "error", seconds=elapsed)
        raise
    elapsed = time.perf_counter() - started
    telemetry.record(
        method.value, binding.kind.value, "ok", seconds=elapsed, size_bytes=output.raw_size_bytes
    )
    return result


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    telemetry: CaptureTelemetry = _DISABLED_TELEMETRY,
) -> None:
    """Bind the `capture_vmcore` job handler."""
    registry.register(
        JobKind.CAPTURE_VMCORE,
        lambda conn, job: capture_handler(conn, job, resolver=resolver, telemetry=telemetry),
    )
