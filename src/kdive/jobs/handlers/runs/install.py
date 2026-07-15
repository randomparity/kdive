"""Worker install handler for the `runs.*` plane."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.idempotency import claim_run_step, complete_run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run, System
from kdive.domain.operations.jobs import Job
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.runs.common import abandon_run_step_best_effort
from kdive.jobs.payloads import InstallPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.kernel_config.gate import crash_capture_refusal
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.lifecycle import Installer, InstallRequest
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.services.runs.steps import (
    cmdline_for,
    existing_build_result,
    install_method_for,
    system_arch,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _InstallPayloadContext:
    run_id: UUID
    override: str | None
    crashkernel: str | None


@dataclass(frozen=True, slots=True)
class _InstallPlan:
    run: Run
    installer: Installer
    request: InstallRequest
    applied_extra: str | None
    crashkernel: str | None


async def install_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Stage the built kernel for direct-kernel boot, recording the `install` step.

    Accepts only ``JobKind.INSTALL`` payloads. The retired ``build_install_boot`` composite no
    longer has a registered worker handler, so compatibility decoding stays out of this boundary.
    """
    payload = _install_payload_context(job)
    plan = await _resolve_install_plan(conn, payload, resolver)
    job_ctx = job_context_from_job(job, plan.run.project)
    claimed = await _run_install_step(conn, payload.run_id, plan.installer, plan.request)
    if not claimed:
        return str(payload.run_id)
    await _complete_install_step(conn, job_ctx, plan)
    return str(payload.run_id)


def _install_payload_context(job: Job) -> _InstallPayloadContext:
    install_payload = load_payload(job, InstallPayload)
    return _InstallPayloadContext(
        run_id=UUID(install_payload.run_id),
        override=install_payload.cmdline,
        crashkernel=install_payload.crashkernel,
    )


async def _resolve_install_plan(
    conn: AsyncConnection,
    payload: _InstallPayloadContext,
    resolver: ProviderResolver,
) -> _InstallPlan:
    run_id = payload.run_id
    run = await RUNS.get(conn, run_id)
    if run is None:
        raise CategorizedError(
            "install target run is gone or unbuilt (no kernel_ref)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )
    kernel_ref = run.kernel_ref
    if kernel_ref is None:
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
    method = install_method_for(system, runtime.profile_policy)
    await _validate_crashkernel(conn, run_id, method, payload.crashkernel)
    return await _build_install_plan(
        conn,
        run,
        system,
        runtime.installer,
        method,
        kernel_ref=kernel_ref,
        root_cmdline=runtime.platform_root_cmdline,
        payload=payload,
    )


async def _validate_crashkernel(
    conn: AsyncConnection,
    run_id: UUID,
    method: CaptureMethod,
    crashkernel: str | None,
) -> None:
    if crashkernel is None:
        return
    if method not in (CaptureMethod.KDUMP, CaptureMethod.FADUMP):
        # Backstop for the tool-boundary gate (ADR-0300): a crashkernel reservation is a
        # kdump-family token (KDUMP/FADUMP, ADR-0349). Fail loudly rather than compose a cmdline
        # that silently drops it — this covers a hand-crafted payload or an accept-then-reprovision
        # skew after the boundary accepted it.
        raise CategorizedError(
            "crashkernel reservation requires a kdump-capture system",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "crashkernel_requires_kdump", "method": method.value},
        )
    # Kernel-config gate (ADR-0318): a crashkernel reservation is useless if the uploaded
    # kernel cannot kdump. Refuse loudly rather than reserve memory for a dump that can never
    # happen; the helper fails open (None) on no upload / read error / degenerate config.
    refusal = await crash_capture_refusal(conn, run_id)
    if refusal is not None:
        raise CategorizedError(
            "uploaded kernel config lacks symbols required for kdump crash capture",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details=dict(refusal),
        )


async def _build_install_plan(
    conn: AsyncConnection,
    run: Run,
    system: System,
    installer: Installer,
    method: CaptureMethod,
    *,
    kernel_ref: str,
    root_cmdline: str | None,
    payload: _InstallPayloadContext,
) -> _InstallPlan:
    run_id = payload.run_id
    # One read of the build step result feeds the cmdline, initrd, and debuginfo below.
    build_result = await existing_build_result(conn, run_id)
    build_extra = build_result.cmdline if build_result is not None else None
    # The applied client extra (ADR-0299): the install override when supplied, else the build-baked
    # extra. Both are already-normalized (payload/build validators strip), so re-stage's equality
    # check against this recorded value is exact. Passing it as the override composes the boot
    # cmdline without cmdline_for re-reading the build result.
    applied_extra = payload.override if payload.override is not None else build_extra
    cmdline = await cmdline_for(
        conn,
        run,
        method,
        root_cmdline=root_cmdline,
        arch=system_arch(system),
        override=applied_extra,
        crashkernel=payload.crashkernel,
    )
    _log.info("install: run %s resolved cmdline %r (method %s)", run_id, cmdline, method.value)
    initrd_ref = build_result.initrd_ref if build_result is not None else None
    debuginfo_ref = build_result.debuginfo_ref if build_result is not None else None
    return _InstallPlan(
        run=run,
        installer=installer,
        request=InstallRequest(
            system_id=system.id,
            run_id=run_id,
            kernel_ref=kernel_ref,
            cmdline=cmdline,
            method=method,
            initrd_ref=initrd_ref,
            debuginfo_ref=debuginfo_ref,
        ),
        applied_extra=applied_extra,
        crashkernel=payload.crashkernel,
    )


async def _run_install_step(
    conn: AsyncConnection,
    run_id: UUID,
    installer: Installer,
    request: InstallRequest,
) -> bool:
    claim = await claim_run_step(conn, run_id, "install")
    if not claim.claimed:
        return False
    try:
        await asyncio.to_thread(installer.install, request)
    except Exception:
        await abandon_run_step_best_effort(conn, run_id, "install")
        raise
    return True


async def _complete_install_step(
    conn: AsyncConnection, job_ctx: RequestContext, plan: _InstallPlan
) -> None:
    run = plan.run
    run_id = run.id
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        await complete_run_step(
            conn,
            run_id,
            "install",
            {
                "system_id": str(plan.request.system_id),
                "cmdline": plan.applied_extra,
                "crashkernel": plan.crashkernel,
            },
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
