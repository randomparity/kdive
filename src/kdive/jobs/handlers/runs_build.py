"""Worker build handler for the `runs.*` plane."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

import kdive.config as config
from kdive.build_artifacts.results import BuildOutput
from kdive.config.core_settings import KERNEL_SRC
from kdive.db import build_hosts
from kdive.db.build_hosts import BuildHost
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import IllegalTransition, RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import Run
from kdive.domain.operations.jobs import Job
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.runs_shared import finalize_build
from kdive.jobs.payloads import BuildPayload, load_payload
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.shared.build_host.dispatch import (
    BuildHostTransportFactories,
    run_build_on_host,
)
from kdive.security import audit
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs.steps import BuildStepResult, existing_build_result

_log = logging.getLogger(__name__)


async def _fail_build(conn: AsyncConnection, job: Job, run: Run, category: ErrorCategory) -> None:
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
            await conn.execute(
                "UPDATE runs SET state = %s, failure_category = %s, failing_job_id = %s "
                "WHERE id = %s AND state = %s",
                (RunState.FAILED.value, category.value, job.id, run.id, RunState.RUNNING.value),
            )
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (run.id,))
                row = await cur.fetchone()
            if row is None or RunState(row["state"]) is not RunState.FAILED:
                raise IllegalTransition(f"run {run.id} was not running at build failure")
            await audit.record(
                conn,
                job_context_from_job(job, run.project),
                audit.AuditEvent(
                    tool="runs.build",
                    object_kind="runs",
                    object_id=run.id,
                    transition="running->failed",
                    args={"run_id": str(run.id)},
                    project=run.project,
                ),
            )
    except IllegalTransition:
        _log.warning(
            "build of run %s failed (%s) but it is already terminal; failure not recorded "
            "on the Run (a concurrent cancel won)",
            run.id,
            category.value,
        )


async def _release_build_lease(conn: AsyncConnection, run_id: UUID) -> None:
    """Delete the run's build-host lease; called only on the SUCCESS path.

    The lease is released only when the build succeeds. On failure it is deliberately retained so
    a retry (BUILD jobs retry up to ``max_attempts``) cannot over-admit the host; the reconciler
    reclaims it when the job is terminal (see ``_build_and_record``).

    Errors are logged and swallowed — the reconciler is the backstop. A worker-local run holds no
    lease, so this is an idempotent no-op DELETE.
    """
    try:
        async with conn.transaction():
            await build_hosts.release_lease(conn, run_id)
    except Exception:
        _log.warning("failed to release build-host lease for run %s", run_id, exc_info=True)


async def _run_build(
    run: Run,
    parsed: ServerBuildProfile,
    *,
    host: BuildHost,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    kernel_src: str,
    transport_factories: BuildHostTransportFactories | None = None,
) -> BuildOutput:
    """Resolve the runtime builder and run it on ``host`` through the build-host seam.

    The builder is selected from ``run.target_kind`` (ADR-0169), not the System join, so a Run
    that has no System bound yet still builds against its committed resource kind.
    """
    run_id = run.id
    builder = resolver.resolve(run.target_kind).builder
    return await run_build_on_host(
        builder,
        host,
        run_id,
        parsed,
        secret_registry=secret_registry,
        kernel_src=kernel_src,
        transport_factories=transport_factories,
    )


async def _resolve_build_host(
    conn: AsyncConnection, payload: BuildPayload, run_id: UUID
) -> BuildHost:
    """Resolve the BUILD payload's admitted host id to a live row.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` when the admitted host row has vanished
            (its lease/host disappeared between admission and build).
    """
    host_id = UUID(payload.build_host_id)
    host = await build_hosts.get_by_id(conn, host_id)
    if host is None:
        raise CategorizedError(
            "selected build host is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id), "build_host_id": str(host_id)},
        )
    return host


async def build_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    transport_factories: BuildHostTransportFactories | None = None,
) -> str | None:
    """Build the Run's kernel on the selected host and drive it `running -> succeeded` or failed.

    The build host is read from the BUILD payload (admitted under capacity at the ``runs.build``
    boundary): a worker-local host runs the resolved runtime builder directly; an ssh host runs a
    transport-bound remote-libvirt builder inside the materialized-identity context manager. The
    capacity lease is released only after ``finalize_build`` succeeds. Categorized build failures
    retain the lease across retries; once the job reaches a terminal state, the reconciler reclaims
    the orphaned build-host lease. A worker-local run holds no lease, so success-path release is a
    harmless no-op there.
    """
    restore_autocommit = False
    if not conn.autocommit:
        if conn.pgconn.transaction_status != TransactionStatus.IDLE:
            await conn.rollback()
        await conn.set_autocommit(True)
        restore_autocommit = True
    try:
        return await _build_handler_autocommit(
            conn,
            job,
            resolver=resolver,
            secret_registry=secret_registry,
            transport_factories=transport_factories,
        )
    finally:
        if restore_autocommit:
            await conn.set_autocommit(False)


async def _build_handler_autocommit(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    transport_factories: BuildHostTransportFactories | None = None,
) -> str | None:
    payload = load_payload(job, BuildPayload)
    run_id = UUID(payload.run_id)
    run = await RUNS.get(conn, run_id)
    if run is None:
        raise CategorizedError(
            "build target run is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id)},
        )
    parsed = BuildProfile.parse(run.build_profile)
    if not isinstance(parsed, ServerBuildProfile):
        raise CategorizedError(
            "external-source run reached the server build handler",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )
    result = await existing_build_result(conn, run_id)
    if result is None:
        result = await _build_and_record(
            conn,
            job,
            run,
            parsed,
            payload,
            resolver=resolver,
            secret_registry=secret_registry,
            transport_factories=transport_factories,
        )
    await finalize_build(conn, job, run, result)
    await _release_build_lease(conn, run_id)
    return str(run_id)


async def _build_and_record(
    conn: AsyncConnection,
    job: Job,
    run: Run,
    parsed: ServerBuildProfile,
    payload: BuildPayload,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    transport_factories: BuildHostTransportFactories | None = None,
) -> BuildStepResult:
    """Resolve the host, run the build, and shape the ledger result; mark FAILED on error.

    The build-host lease is **not** released on the failure path. BUILD jobs retry
    (``max_attempts=3``; ``queue.fail`` requeues non-terminally), and the handler rebuilds on
    every attempt while ``existing_build_result`` is ``None``. Releasing the slot here would free
    it between attempts, letting another build grab it while attempts 2-3 still run on the host —
    ``max_concurrent`` over-admission. Instead the lease is held until the job is terminal: the
    reconciler's :func:`reclaim_orphan_build_host_leases` reclaims it (keyed on job liveness) once
    the job is dead-lettered after the last attempt. Only the success path releases the lease.
    """
    run_id = run.id
    try:
        host = await _resolve_build_host(conn, payload, run_id)
        kernel_src = config.get(KERNEL_SRC) or ""
        output = await _run_build(
            run,
            parsed,
            host=host,
            resolver=resolver,
            secret_registry=secret_registry,
            kernel_src=kernel_src,
            transport_factories=transport_factories,
        )
    except CategorizedError as exc:
        await _fail_build(conn, job, run, exc.category)
        raise
    return BuildStepResult(
        kernel_ref=output.kernel_ref,
        debuginfo_ref=output.debuginfo_ref,
        build_id=output.build_id,
        cmdline=payload.cmdline,
    )
