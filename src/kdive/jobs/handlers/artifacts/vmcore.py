"""Worker handlers for the `vmcore.*` retrieve plane."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.read_model import raw_vmcore_key, redacted_vmcore_artifact_id
from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import HeadResult, StoredArtifact
from kdive.artifacts.upload_manifest import RUN_UPLOAD_OWNER
from kdive.artifacts.write_lease import hold_write_lease, release_write_lease
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS, SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run, System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.console.capture_telemetry import CaptureOutcome, CaptureTelemetry
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import CaptureVmcorePayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.shared.host_dump_volume_leases import (
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
    """Under the per-Run lock, return an existing same-method capture, or the Run + bound System.

    Run-addressed (ADR-0244): the core is owned by the crashing Run, so the dedup guard and the
    advisory lock are scoped to ``run_id``. ``system`` is resolved from the Run's binding so the
    provider can locate the live domain/overlay/volume. The dedup guard reads the *raw* key (only
    it carries the capture method for :func:`ensure_method_match`), but the replay hands back the
    *redacted* artifact id — the reference the job result carries (ADR-0466).
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
            return ExistingCapture(await redacted_vmcore_artifact_id(conn, run_id))
        return run, system


async def verify_objects_still_stored(
    store: CaptureObjectStore, run_id: UUID, *stored: StoredArtifact
) -> None:
    """Raise unless the store still holds each ``stored`` object at the etag the capture observed.

    This is the ADR-0497 fence against any lost or replaced write. ADR-0524's upload-orphan sweep
    now captures immutable VersionIds before its final database fence and deletes only those
    identities after unlocking, so a later PUT at this deterministic key survives cleanup. This
    verification remains the publication backstop for disappearance or replacement from any other
    path before the artifact row commits.

    The comparison is on the etag rather than on presence, which costs the same round trip and
    catches strictly more: an object deleted *and re-PUT* between the capture and here is present
    but is not the object the row would claim, and committing would write an ``etag`` column that
    never matched any bytes.

    The guard does not attempt cleanup or repair. It fails the finalize transaction with the key
    and observed identity so a retry can publish a fresh capture without creating a dangling row.

    Args:
        store: The object store to stat through.
        run_id: The owning Run, for the error details.
        stored: Every object whose ``artifacts`` row is about to be committed.

    Raises:
        CategorizedError: an object is absent or holds different bytes
            (:attr:`~kdive.domain.errors.ErrorCategory.INFRASTRUCTURE_FAILURE`). Raised inside the
            caller's transaction so no row survives.
    """
    for artifact in stored:
        head = await asyncio.to_thread(store.head, artifact.key)
        details: dict[str, object] = {
            "run_id": str(run_id),
            "object_key": artifact.key,
            "captured_etag": artifact.etag,
        }
        if head is None:
            raise CategorizedError(
                "the captured object is gone from the object store; no artifact row is committed "
                "for this capture. Re-run the capture.",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details=details,
            )
        if head.etag != artifact.etag:
            raise CategorizedError(
                "the captured object was replaced in the object store after it was captured; no "
                "artifact row is committed for this capture. Re-run the capture.",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details=details | {"stored_etag": head.etag},
            )


async def finalize_capture(
    conn: AsyncConnection,
    job: Job,
    run: Run,
    method: CaptureMethod,
    output: Any,
    *,
    artifact_store: CaptureObjectStore,
) -> str | None:
    """Insert both Run-owned artifact rows + audit under the per-Run lock (ADR-0244).

    Returns the redacted artifact's id — the job's result reference (ADR-0466). A concurrent
    handler that already landed the core wins the re-check and its redacted id is returned
    instead; ``None`` only when that raced core has no surviving redacted row.

    Both objects are re-stated against the store before this transaction commits
    (:func:`verify_objects_still_stored`, ADR-0497). It is the **last** thing the transaction does,
    and that ordering is the whole point: the orphan sweep's per-key re-check reads *committed*
    rows, so an uncommitted insert protects nothing and the object becomes safe only at the commit.
    Heading immediately before it therefore narrows the window in which the sweep can delete under
    this handler from the length of a multi-GiB PUT to a HEAD plus a commit round trip. The replay
    arm needs no verify: it commits no new row, and the object it returns an id for is already
    row-protected.

    Since ADR-0502 the verify is a backstop rather than the only guard: this transaction also
    releases the write lease ``capture_handler`` minted before the capture, so the sweep was fenced
    off the key for the whole PUT and the fence lifts in the same commit that makes the object
    row-protected. The verify stays because the lease covers only the writers that take one, and
    because it is what turns any lost write — leased or not — into a failed job rather than a
    dangling row.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        # Released here rather than after the inserts so both arms drop it, and inside this
        # transaction so it lands atomically with whatever the arm commits: a verify that raises
        # below rolls the release back with the rows, leaving the lease standing over objects that
        # gained no ``artifacts`` row (ADR-0502). The replay arm commits no row but needs the
        # release too — the core it found is already row-protected, and its own write reused that
        # same key.
        await release_write_lease(conn, RUN_UPLOAD_OWNER, run.id, job.id)
        # The dump-volume lease goes with it (ADR-0562). Released unconditionally rather than under
        # the same ``host_dump`` test that gates the mint, so the gate lives in one place and
        # narrowing it later cannot strand a row: with no lease the DELETE matches nothing. A Run
        # with no System binding never reached the mint, and both arms release for the write lease's
        # reason — the replay arm's own earlier attempt held one over the same volume.
        if run.system_id is not None:
            await release_host_dump_volume_lease(conn, run.system_id, job.id)
        existing = await raw_vmcore_key(conn, run.id)
        if existing is not None:
            ensure_method_match(existing, method, run.id)
            return await redacted_vmcore_artifact_id(conn, run.id)
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.raw, owner_kind="runs", owner_id=run.id)
        )
        redacted = await ARTIFACTS.insert(
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
        await verify_objects_still_stored(artifact_store, run.id, output.raw, output.redacted)
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
    :func:`verify_objects_still_stored` stats through, and a ``None`` default that skipped the
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
    # Declared and committed *before* the first byte, because the ADR-0455 orphan sweep fences on
    # committed rows: this is the only thing standing between a multi-GiB ``put_stream`` under
    # ``local/runs/`` and a sweep that decides the deterministic key reclaimable and deletes it
    # (ADR-0502). It is not released on the failure path below — see ``reap_stale_write_leases``.
    #
    # It goes *here*, ahead of the resolver, and not merely for tidiness: ``precheck_run`` has just
    # committed, so the connection is transaction-free and the mint's own transaction is a real one.
    # One statement earlier — the resolver's read is enough — leaves a non-autocommit connection in
    # an open transaction, and then the mint degrades to a savepoint that commits nothing until this
    # handler returns while holding ``LockScope.RUN`` across the whole capture. ``hold_write_lease``
    # raises rather than allowing that, so the ordering cannot rot silently.
    await hold_write_lease(conn, RUN_UPLOAD_OWNER, run.id, job.id)
    # The second declaration, over the System rather than the object prefix (ADR-0562). Only
    # is the only method that creates ``kdive-host-dump-<system_id>.kdump``, so it is the only one
    # that needs to fence the reconciler's orphaned-volume sweep off that name; a kdump/gdbstub/
    # console capture mints nothing. It sits here, after the write lease and before the resolver's
    # read, for the same connection-state reason: ``hold_write_lease`` has just committed, so this
    # mint's own transaction is a real one rather than a savepoint that commits nothing and holds
    # ``LockScope.SYSTEM`` across the whole capture. Both mints assert that rather than assume it.
    if method is CaptureMethod.HOST_DUMP:
        await hold_host_dump_volume_lease(conn, system.id, job.id)
    binding = await resolver.binding_for_system(conn, system.id)
    set_provider_kind(binding.kind.value)
    retriever = binding.runtime.retriever
    started = time.perf_counter()
    try:
        output = await asyncio.to_thread(retriever.capture, system.id, run.id, method)
        result = await finalize_capture(
            conn, job, run, method, output, artifact_store=artifact_store
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
