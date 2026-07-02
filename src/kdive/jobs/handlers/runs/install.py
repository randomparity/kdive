"""Worker install handler for the `runs.*` plane."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.idempotency import claim_run_step, complete_run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.runs.common import abandon_run_step_best_effort
from kdive.jobs.payloads import InstallPayload, RunPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.lifecycle import InstallRequest
from kdive.security import audit
from kdive.services.runs.steps import (
    cmdline_for,
    existing_build_result,
    install_method_for,
)

_log = logging.getLogger(__name__)


async def install_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Stage the built kernel for direct-kernel boot, recording the `install` step.

    Serves two callers: a standalone ``JobKind.INSTALL`` job (which may carry a ``cmdline``
    override, ADR-0299) and the composite ``build_install_boot`` install phase (a
    ``JobKind.BUILD_INSTALL_BOOT`` job whose ``run_only`` payload bakes its cmdline at build, so it
    carries no install-time override). The override is read only for a genuine ``INSTALL`` job;
    ``load_payload`` cannot decode an ``InstallPayload`` from the composite's other-kinded job.
    """
    if job.kind is JobKind.INSTALL:
        install_payload = load_payload(job, InstallPayload)
        run_id = UUID(install_payload.run_id)
        override = install_payload.cmdline
    else:
        run_id = UUID(load_payload(job, RunPayload).run_id)
        override = None
    run = await RUNS.get(conn, run_id)
    if run is None or run.kernel_ref is None:
        raise CategorizedError(
            "install target run is gone or unbuilt (no kernel_ref)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )
    system_id = run.require_system_id()
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "install target system is gone",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id), "system_id": str(system_id)},
        )
    binding = await resolver.binding_for_system(conn, system_id)
    set_provider_kind(binding.kind.value)
    runtime = binding.runtime
    installer = runtime.installer
    method = install_method_for(system, runtime.profile_policy)
    kernel_ref = run.kernel_ref
    # One read of the build step result feeds the cmdline, initrd, and debuginfo below.
    build_result = await existing_build_result(conn, run_id)
    build_extra = build_result.cmdline if build_result is not None else None
    # The applied client extra (ADR-0299): the install override when supplied, else the build-baked
    # extra. Both are already-normalized (payload/build validators strip), so re-stage's equality
    # check against this recorded value is exact. Passing it as the override composes the boot
    # cmdline without cmdline_for re-reading the build result.
    applied_extra = override if override is not None else build_extra
    cmdline = await cmdline_for(
        conn, run, method, root_cmdline=runtime.platform_root_cmdline, override=applied_extra
    )
    _log.info("install: run %s resolved cmdline %r (method %s)", run_id, cmdline, method.value)
    initrd_ref = build_result.initrd_ref if build_result is not None else None
    debuginfo_ref = build_result.debuginfo_ref if build_result is not None else None
    job_ctx = job_context_from_job(job, run.project)
    claim = await claim_run_step(conn, run_id, "install")
    if not claim.claimed:
        return str(run_id)
    try:
        await asyncio.to_thread(
            installer.install,
            InstallRequest(
                system_id=system_id,
                run_id=run_id,
                kernel_ref=kernel_ref,
                cmdline=cmdline,
                method=method,
                initrd_ref=initrd_ref,
                debuginfo_ref=debuginfo_ref,
            ),
        )
    except Exception:
        await abandon_run_step_best_effort(conn, run_id, "install")
        raise
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        await complete_run_step(
            conn, run_id, "install", {"system_id": str(system_id), "cmdline": applied_extra}
        )
        await audit.record(
            conn,
            job_ctx,
            audit.AuditEvent(
                tool="runs.install",
                object_kind="runs",
                object_id=run_id,
                transition="install",
                args={"run_id": str(run_id)},
                project=run.project,
            ),
        )
    return str(run_id)
