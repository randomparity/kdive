"""Plain async handlers for the vmcore and postmortem MCP surface."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import CaptureVmcorePayload
from kdive.kernel_config.gate import crash_capture_refusal
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import ConfigErrorReason, job_envelope
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import capability_unsupported as _capability_unsupported
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._idempotency import keyed_mutation
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run
from kdive.mcp.tools._vmcore_targets import resolve_run_vmcore_target, vmcore_target_failure
from kdive.mcp.tools.lifecycle.vmcore_view import (
    console_crash_redirect,
    postmortem_success_response,
    triage_response,
    vmcore_collection,
)
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.ports.retrieve import CrashPostmortem
from kdive.security.artifacts.crash_commands import validate_crash_commands
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.artifacts.listing import list_redacted_run_artifacts

_TRIAGE_COMMANDS: tuple[str, ...] = ("log", "bt")

# Idempotency-store kind for vmcore.fetch (the registered tool name); ADR-0193.
_VMCORE_FETCH_KIND = "vmcore.fetch"

# The core-producing methods valid for vmcore.fetch (excludes console/gdbstub).
_VMCORE_METHODS: frozenset[CaptureMethod] = frozenset(
    {CaptureMethod.HOST_DUMP, CaptureMethod.KDUMP}
)


@dataclass(frozen=True, slots=True)
class VmcoreHandlers:
    """vmcore/postmortem MCP handlers with provider seams bound at construction."""

    resolver: ProviderResolver
    secret_registry: SecretRegistry

    async def fetch_vmcore(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        run_id: str,
        method: CaptureMethod | str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolResponse:
        return await with_runtime_for_run(
            pool,
            self.resolver,
            ctx,
            run_id,
            lambda runtime: _fetch_vmcore(
                pool,
                ctx,
                run_id=run_id,
                method=method,
                runtime=runtime,
                idempotency_key=idempotency_key,
            ),
            required_role=Role.CONTRIBUTOR,
        )

    async def postmortem_crash(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        run_id: str,
        commands: list[str],
    ) -> ToolResponse:
        return await self._with_postmortem_crash_port(
            pool,
            ctx,
            run_id,
            lambda crash, secret_registry: _postmortem_crash(
                pool,
                ctx,
                run_id=run_id,
                commands=commands,
                crash=crash,
                secret_registry=secret_registry,
            ),
        )

    async def postmortem_triage(
        self, pool: AsyncConnectionPool, ctx: RequestContext, *, run_id: str
    ) -> ToolResponse:
        return await self._with_postmortem_crash_port(
            pool,
            ctx,
            run_id,
            lambda crash, secret_registry: _postmortem_triage(
                pool,
                ctx,
                run_id=run_id,
                crash=crash,
                secret_registry=secret_registry,
            ),
        )

    async def _with_postmortem_crash_port(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        run_id: str,
        invoke_postmortem: Callable[[CrashPostmortem, SecretRegistry], Awaitable[ToolResponse]],
    ) -> ToolResponse:
        return await with_runtime_for_run(
            pool,
            self.resolver,
            ctx,
            run_id,
            lambda runtime: invoke_postmortem(runtime.crash_postmortem, self.secret_registry),
            required_role=Role.CONTRIBUTOR,
        )


def _supported_method_values(runtime: ProviderRuntime) -> list[str]:
    """Provider's supported core methods as sorted tokens for an error's ``supported`` set."""
    return [m.value for m in sorted(runtime.support.capture_methods, key=lambda m: m.value)]


def _resolve_capture_method(
    object_id: str,
    method: CaptureMethod | str | None,
    system: System,
    runtime: ProviderRuntime,
) -> CaptureMethod | ToolResponse:
    """Resolve the capture method to admit, or a typed rejection (ADR-0209).

    ``object_id`` is the Run id the envelope is keyed on (ADR-0244). An explicit method must parse,
    be core-producing (``_VMCORE_METHODS``), and be advertised by the bound provider's descriptor
    (else ``capability_unsupported``). An omitted method resolves through the System profile's
    ``capture_method`` clamped to the core-producing set; if that yields no descriptor-supported
    core method the call needs an explicit ``method`` (``missing_required_field`` — the provider may
    support core methods, the caller omitted one).
    """
    if method is not None:
        try:
            explicit = method if isinstance(method, CaptureMethod) else CaptureMethod(method)
        except ValueError:
            return _config_error(
                object_id, data={"method": method, "reason": "unknown capture method"}
            )
        if explicit not in _VMCORE_METHODS:
            return _config_error(
                object_id, data={"method": method, "reason": "method does not produce a vmcore"}
            )
        if explicit not in runtime.support.capture_methods:
            return _capability_unsupported(
                object_id,
                capability=f"capture_method:{explicit.value}",
                provider=runtime.support.component_sources.provider,
                supported=_supported_method_values(runtime),
            )
        return explicit
    try:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(object_id, exc)
    resolved = runtime.profile_policy.capture_method(profile)
    if resolved in _VMCORE_METHODS and resolved in runtime.support.capture_methods:
        return resolved
    return _config_error_reason(
        object_id,
        ConfigErrorReason.MISSING_REQUIRED_FIELD,
        accepted_values=_supported_method_values(runtime),
        detail=(
            "this System's profile resolves to no implicit core capture method; "
            "pass an explicit core-producing method"
        ),
    )


async def _fetch_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    method: CaptureMethod | str | None = None,
    runtime: ProviderRuntime,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit a `capture_vmcore` job for a Run whose bound System is `crashed` (contributor).

    Run-addressed (ADR-0244): the core is owned by the crashing Run. The handler resolves the Run,
    derives its bound System, and applies the unchanged precondition — the System must be `CRASHED`.
    The capture method is admitted against the bound provider's ADR-0208 descriptor (ADR-0209): an
    explicit method the provider does not support is rejected with ``capability_unsupported``; an
    omitted method resolves through the System profile's ``ProfilePolicy.capture_method`` clamped to
    the core-producing set, and a profile yielding no implicit core method requires an explicit
    ``method``. No job row is created on any rejection.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.CONTRIBUTOR)
            if run.system_id is None:
                return _config_error(
                    run_id,
                    detail="run is not bound to a system; cannot capture a vmcore",
                    data={"reason": "run_unbound"},
                )
            system = await SYSTEMS.get(conn, run.system_id)
            if system is None:
                return _config_error(run_id)
            if system.state is not SystemState.CRASHED:
                return _config_error(
                    run_id,
                    detail=(
                        "system must be in CRASHED state to capture a vmcore; current state = "
                        f"{system.state.value}"
                    ),
                    data={"current_status": system.state.value},
                )

            resolved = _resolve_capture_method(run_id, method, system, runtime)
            if isinstance(resolved, ToolResponse):
                return resolved
            capture_method = resolved

            if capture_method is CaptureMethod.KDUMP:
                # Kernel-config gate (ADR-0318): a kdump vmcore is produced by the guest kernel, so
                # it needs the crash_capture symbols. host_dump is host-side (QEMU) and never gates.
                # crash_capture_refusal fails open (None) on no upload / read error / degenerate.
                refusal = await crash_capture_refusal(conn, uid)
                if refusal is not None:
                    return _config_error(
                        run_id,
                        detail="uploaded kernel config lacks symbols required for a kdump vmcore",
                        data=refusal,
                    )

            async def _enqueue() -> ToolResponse:
                job = await queue.enqueue(
                    conn,
                    JobKind.CAPTURE_VMCORE,
                    CaptureVmcorePayload(run_id=run_id, method=capture_method),
                    job_authorizing(ctx, run.project),
                    f"{run_id}:capture_vmcore:{capture_method.value}",
                )
                return job_envelope(job, "run_id", uid)

            return await keyed_mutation(
                conn,
                idempotency_key=idempotency_key,
                principal=ctx.principal,
                project=run.project,
                kind=_VMCORE_FETCH_KIND,
                do_work=_enqueue,
            )


async def list_vmcores(
    pool: AsyncConnectionPool, ctx: RequestContext, *, run_id: str
) -> ToolResponse:
    """Return the Run's `redacted` vmcore artifacts in one collection envelope (ADR-0244)."""
    listed = await list_redacted_run_artifacts(pool, ctx, run_id=run_id)
    return vmcore_collection(run_id, listed)


async def _postmortem_crash(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    commands: list[str],
    crash: CrashPostmortem,
    secret_registry: SecretRegistry,
) -> ToolResponse:
    """Run the crash command batch over the viewer-authorized Run's captured core."""
    with bind_context(principal=ctx.principal):
        if validate_crash_commands(commands) is not None:
            return _config_error(run_id)
        async with pool.connection() as conn:
            try:
                resolved = await resolve_run_vmcore_target(conn, ctx, run_id)
            except CategorizedError as exc:
                redirect = console_crash_redirect(run_id, exc)
                return redirect if redirect is not None else vmcore_target_failure(run_id, exc)
        try:
            output = await asyncio.to_thread(
                crash.run_crash_postmortem,
                vmcore_ref=resolved.vmcore_ref,
                debuginfo_ref=resolved.debuginfo_ref,
                expected_build_id=resolved.build_id,
                commands=commands,
            )
        except CategorizedError as exc:
            # A provenance mismatch (configuration_error) or an unavailable crash
            # dependency (missing_dependency) becomes a typed failure, never a 500.
            return ToolResponse.failure_from_error(run_id, exc)
        return postmortem_success_response(
            run_id,
            transcript=output.transcript,
            truncated=output.truncated,
            secret_registry=secret_registry,
        )


async def _postmortem_triage(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    crash: CrashPostmortem,
    secret_registry: SecretRegistry,
) -> ToolResponse:
    """Run the fixed triage command batch and return the redacted report."""
    resp = await _postmortem_crash(
        pool,
        ctx,
        run_id=run_id,
        commands=list(_TRIAGE_COMMANDS),
        crash=crash,
        secret_registry=secret_registry,
    )
    if resp.status == "error":
        return resp
    return triage_response(resp)
