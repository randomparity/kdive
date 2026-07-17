"""Read-side `runs.get` MCP handler."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS, RUNS, SYSTEMS
from kdive.domain.capacity.state import RunState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.records import Run, System
from kdive.domain.lifecycle.run_steps import RUN_STEP_SUCCEEDED
from kdive.domain.operations.jobs import Job
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools.lifecycle.runs.common import envelope_for_run
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.artifacts.listing import ConsoleManifest, list_run_console_artifacts
from kdive.services.debug.sessions import active_session_ids_for_run
from kdive.services.runs.liveness import Liveness, derive_liveness
from kdive.services.runs.steps import (
    READY_BOOT_OUTCOME,
    BootAttempt,
    BuildStepResult,
    StepProgress,
    system_required_cmdline,
)
from kdive.services.runs.steps import existing_build_result as _existing_build_result
from kdive.services.runs.steps import failed_boot_attempt as _failed_boot_attempt
from kdive.services.runs.steps import install_method_for as _install_method_for
from kdive.services.runs.steps import step_progress as _step_progress
from kdive.services.runs.steps import system_arch as _system_arch


@dataclass(frozen=True, slots=True)
class RunReadDetails:
    """Optional read enrichments for a ``runs.get`` response."""

    required_cmdline: str | None
    failing_job: Job | None
    active_debug_session_ids: list[str]
    step_progress: StepProgress | None
    boot_readiness: BootAttempt | None
    build_result: BuildStepResult | None
    console_manifest: ConsoleManifest | None
    liveness: Liveness | None


async def get_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    include_console_artifacts: bool = False,
) -> ToolResponse:
    """Return a Run the caller's project owns, advertising the boot's required cmdline.

    The Run-scoped console manifest (`data.console_artifacts`, ADR-0279) is opt-in: it is fetched
    and inlined only when ``include_console_artifacts`` is true (#1067, ADR-0324). By default the
    manifest is neither queried nor rendered — the boot snapshot stays at ``refs.console``.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _not_found(run_id)
            require_role(ctx, run.project, Role.VIEWER)
            details = await _load_run_read_details(
                conn,
                run,
                resolver=resolver,
                secret_registry=secret_registry,
                include_console_artifacts=include_console_artifacts,
            )
        return envelope_for_run(
            run,
            required_cmdline=details.required_cmdline,
            failing_job=details.failing_job,
            active_debug_session_ids=details.active_debug_session_ids,
            step_progress=details.step_progress,
            boot_readiness=details.boot_readiness,
            build_provenance=(
                details.build_result.build_provenance if details.build_result is not None else None
            ),
            console_manifest=details.console_manifest,
            liveness=details.liveness,
        )


async def _load_run_read_details(
    conn: AsyncConnection,
    run: Run,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    include_console_artifacts: bool,
) -> RunReadDetails:
    system = await SYSTEMS.get(conn, run.system_id) if run.system_id is not None else None
    runtime = await resolver.runtime_for_run(conn, run.id) if system is not None else None
    progress = await _step_progress(conn, run.id) if run.state is RunState.SUCCEEDED else None
    return RunReadDetails(
        required_cmdline=_required_cmdline(system, runtime),
        failing_job=await _failing_job(conn, run),
        active_debug_session_ids=await active_session_ids_for_run(conn, run.id),
        step_progress=progress,
        boot_readiness=await _boot_readiness(conn, run, progress),
        build_result=await _build_result(conn, run),
        console_manifest=await _console_manifest(conn, run, include_console_artifacts),
        liveness=await _liveness(conn, run, progress, secret_registry),
    )


async def _liveness(
    conn: AsyncConnection,
    run: Run,
    progress: StepProgress | None,
    secret_registry: SecretRegistry,
) -> Liveness | None:
    # Gated to a ready-booted local-libvirt Run — the only place a "healthy vs wedged" question is
    # meaningful, and the only provider whose console log and loopback SSH forward live on this host
    # (ADR-0373). Mirrors the data.boot_outcome gate in envelope_for_run.
    if (
        run.system_id is None
        or run.target_kind is not ResourceKind.LOCAL_LIBVIRT
        or progress is None
        or progress.boot_outcome != READY_BOOT_OUTCOME
    ):
        return None
    return await derive_liveness(conn, run.system_id, secret_registry)


def _required_cmdline(system: System | None, runtime: ProviderRuntime | None) -> str | None:
    if system is None or runtime is None:
        return None
    return system_required_cmdline(
        _install_method_for(system, runtime.profile_policy),
        runtime.platform_root_cmdline,
        arch=_system_arch(system),
    )


async def _failing_job(conn: AsyncConnection, run: Run) -> Job | None:
    if run.state is not RunState.FAILED or run.failing_job_id is None:
        return None
    return await JOBS.get(conn, run.failing_job_id)


async def _boot_readiness(
    conn: AsyncConnection, run: Run, progress: StepProgress | None
) -> BootAttempt | None:
    if progress is None or progress.boot == RUN_STEP_SUCCEEDED:
        return None
    return await _failed_boot_attempt(conn, run.id)


async def _build_result(conn: AsyncConnection, run: Run) -> BuildStepResult | None:
    if run.state is not RunState.SUCCEEDED:
        return None
    return await _existing_build_result(conn, run.id)


async def _console_manifest(
    conn: AsyncConnection, run: Run, include_console_artifacts: bool
) -> ConsoleManifest | None:
    # The Run-scoped console manifest (ADR-0279), opt-in per #1067/ADR-0324. Queried inside the
    # open connection (it closes before envelope_for_run) only when the caller asked; always
    # skipped for a failed Run, whose envelope omits it.
    if not include_console_artifacts or run.state is RunState.FAILED:
        return None
    return await list_run_console_artifacts(conn, run.id)
