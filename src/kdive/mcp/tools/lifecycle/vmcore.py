"""The `vmcore.*` / `postmortem.*` MCP tools (ADR-0031).

`vmcore.fetch(system_id, method)` admits a `capture_vmcore` job on a `crashed` System
(dedup `{system_id}:capture_vmcore:{method}`). Worker-owned capture execution lives in
``kdive.jobs.handlers.vmcore``; `vmcore.list` is a `redacted`-only read.
`postmortem.crash`/`.triage` are synchronous, viewer-gated offline reads over an
authorized Run. They resolve the Run's `debuginfo_ref` and captured vmcore through the
shared target resolver, validate caller commands against the allowlist, run the
`CrashPostmortem` port over the captured core, and redact output before returning it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import System
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import CaptureVmcorePayload
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import (
    ConfigErrorReason,
    job_envelope,
)
from kdive.mcp.tools._common import (
    as_uuid as _as_uuid,
)
from kdive.mcp.tools._common import (
    authorizing as job_authorizing,
)
from kdive.mcp.tools._common import (
    capability_unsupported as _capability_unsupported,
)
from kdive.mcp.tools._common import (
    config_error as _config_error,
)
from kdive.mcp.tools._common import (
    config_error_reason as _config_error_reason,
)
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run, with_runtime_for_system
from kdive.mcp.tools._vmcore_targets import (
    CONSOLE_CRASH,
    EXPECTED_CONSOLE_CRASH,
    NO_VMCORE,
    resolve_run_vmcore_target,
    vmcore_target_failure,
)
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.ports import CrashPostmortem
from kdive.security.artifacts.crash_commands import validate_crash_commands
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.artifacts.listing import RedactedArtifact, list_redacted_system_artifacts
from kdive.services.idempotency.envelope import keyed_mutation

_log = logging.getLogger(__name__)

_TRIAGE_COMMANDS: tuple[str, ...] = ("log", "bt")

# Author-controlled narrative for the early-boot console-crash redirect (#734, ADR-0227). For a
# Run that declared expected_boot_failure=console_crash, the kernel panics before kdump's capture
# kernel is loaded via kexec, so kdump never produces a vmcore — none is expected by design, and
# the console artifact is the evidence source. One shared constant so the wording cannot drift; it
# interpolates no guest output, secret, or caller-supplied identifier.
CONSOLE_CRASH_GUIDANCE = (
    "this run declared an early-boot console_crash: the kernel panicked before the kdump "
    "capture kernel was loaded via kexec, so no vmcore is produced and none is expected. "
    "Read the console artifact instead — fetch its reference with runs.get."
)

# Idempotency-store kind for vmcore.fetch (the registered tool name); ADR-0193.
_VMCORE_FETCH_KIND = "vmcore.fetch"


# --- vmcore.fetch (admission) --------------------------------------------------------------


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
        system_id: str,
        method: CaptureMethod | str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolResponse:
        return await with_runtime_for_system(
            pool,
            self.resolver,
            ctx,
            system_id,
            lambda runtime: _fetch_vmcore(
                pool,
                ctx,
                system_id=system_id,
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
        run: Callable[[CrashPostmortem, SecretRegistry], Awaitable[ToolResponse]],
    ) -> ToolResponse:
        return await with_runtime_for_run(
            pool,
            self.resolver,
            ctx,
            run_id,
            lambda runtime: run(runtime.crash_postmortem, self.secret_registry),
            required_role=Role.CONTRIBUTOR,
        )


def _supported_method_values(runtime: ProviderRuntime) -> list[str]:
    """Provider's supported core methods as sorted tokens for an error's ``supported`` set."""
    return [m.value for m in sorted(runtime.supported_capture_methods, key=lambda m: m.value)]


def _resolve_capture_method(
    system_id: str,
    method: CaptureMethod | str | None,
    system: System,
    runtime: ProviderRuntime,
) -> CaptureMethod | ToolResponse:
    """Resolve the capture method to admit, or a typed rejection (ADR-0209).

    An explicit method must parse, be core-producing (``_VMCORE_METHODS``), and be advertised by
    the bound provider's descriptor (else ``capability_unsupported``). An omitted method resolves
    through the System profile's ``capture_method`` clamped to the core-producing set; if that
    yields no descriptor-supported core method the call needs an explicit ``method``
    (``missing_required_field`` — the provider may support core methods, the caller omitted one).
    """
    if method is not None:
        try:
            explicit = method if isinstance(method, CaptureMethod) else CaptureMethod(method)
        except ValueError:
            return _config_error(
                system_id, data={"method": method, "reason": "unknown capture method"}
            )
        if explicit not in _VMCORE_METHODS:
            return _config_error(
                system_id, data={"method": method, "reason": "method does not produce a vmcore"}
            )
        if explicit not in runtime.supported_capture_methods:
            return _capability_unsupported(
                system_id,
                capability=f"capture_method:{explicit.value}",
                provider=runtime.component_sources.provider,
                supported=_supported_method_values(runtime),
            )
        return explicit
    try:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(system_id, exc)
    resolved = runtime.profile_policy.capture_method(profile)
    if resolved in _VMCORE_METHODS and resolved in runtime.supported_capture_methods:
        return resolved
    return _config_error_reason(
        system_id,
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
    system_id: str,
    method: CaptureMethod | str | None = None,
    runtime: ProviderRuntime,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit a `capture_vmcore` job on a `crashed` System (contributor); return the job handle.

    The capture method is admitted against the bound provider's ADR-0208 descriptor (ADR-0209):
    an explicit method the provider does not support is rejected up front with
    ``capability_unsupported``; an omitted method is resolved through the System profile's
    ``ProfilePolicy.capture_method`` clamped to the core-producing set, and a System whose profile
    yields no implicit core method requires an explicit ``method``. No job row is created on any
    rejection.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.CONTRIBUTOR)
            if system.state is not SystemState.CRASHED:
                return _config_error(
                    system_id,
                    detail=(
                        "system must be in CRASHED state to capture a vmcore; current state = "
                        f"{system.state.value}"
                    ),
                    data={"current_status": system.state.value},
                )

            resolved = _resolve_capture_method(system_id, method, system, runtime)
            if isinstance(resolved, ToolResponse):
                return resolved
            capture_method = resolved

            async def _enqueue() -> ToolResponse:
                job = await queue.enqueue(
                    conn,
                    JobKind.CAPTURE_VMCORE,
                    CaptureVmcorePayload(system_id=system_id, method=capture_method),
                    job_authorizing(ctx, system.project),
                    f"{system_id}:capture_vmcore:{capture_method.value}",
                )
                return job_envelope(job, "system_id", uid)

            return await keyed_mutation(
                conn,
                idempotency_key=idempotency_key,
                principal=ctx.principal,
                project=system.project,
                kind=_VMCORE_FETCH_KIND,
                do_work=_enqueue,
            )


# --- vmcore.list ---------------------------------------------------------------------------


def _is_redacted_vmcore(object_key: str) -> bool:
    return "/vmcore-" in object_key and object_key.endswith("-redacted")


async def list_vmcores(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> ToolResponse:
    """Return the System's `redacted` vmcore artifacts in one collection envelope."""
    listed = await list_redacted_system_artifacts(pool, ctx, system_id=system_id)
    items = [_vmcore_item(row) for row in listed if _is_redacted_vmcore(row.object_key)]
    return ToolResponse.collection(
        system_id,
        "ok",
        items,
        suggested_next_actions=["artifacts.get", "postmortem.crash"],
    )


def _vmcore_item(artifact: RedactedArtifact) -> ToolResponse:
    return ToolResponse.success(
        artifact.id,
        "available",
        suggested_next_actions=["artifacts.get"],
        refs={"object": artifact.object_key},
    )


# --- postmortem.crash / .triage ------------------------------------------------------------


def _console_crash_redirect(run_id: str, exc: CategorizedError) -> ToolResponse | None:
    """The early-boot console-crash redirect, or ``None`` to fall through (#734, ADR-0227).

    Fires only when the resolver miss is ``no_vmcore`` **and** the Run declared
    ``expected_boot_failure=console_crash`` (carried on the error's ``details`` by the resolver).
    Returns a ``configuration_error`` — not the suppressed ``not_found`` the bare miss would yield
    — so the author-controlled narrative ``detail`` reaches the caller and points it at the
    console artifact via ``runs.get``. Every other miss returns ``None`` (the handler then keeps
    the existing reason-keyed ``vmcore_target_failure`` envelope unchanged).
    """
    if exc.details.get("reason") != NO_VMCORE:
        return None
    if exc.details.get("expected_boot_failure") != CONSOLE_CRASH:
        return None
    return ToolResponse.failure(
        run_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=CONSOLE_CRASH_GUIDANCE,
        suggested_next_actions=["runs.get", "artifacts.list"],
        data={"reason": EXPECTED_CONSOLE_CRASH, "expected_boot_failure": CONSOLE_CRASH},
    )


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
                redirect = _console_crash_redirect(run_id, exc)
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
        redactor = Redactor(registry=secret_registry)
        return ToolResponse.success(
            run_id,
            "succeeded",
            suggested_next_actions=["postmortem.crash", "artifacts.list"],
            data={"transcript": redactor.redact_text(output.transcript)},
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
    return resp.model_copy(
        update={"suggested_next_actions": ["postmortem.triage", "artifacts.list"]}
    )


# --- registration --------------------------------------------------------------------------


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `vmcore.*` / `postmortem.*` tools on ``app``, bound to ``pool``."""
    handlers = VmcoreHandlers(resolver=resolver, secret_registry=secret_registry)

    @app.tool(
        name="vmcore.fetch",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def vmcore_fetch(
        system_id: Annotated[str, Field(description="The crashed System whose vmcore to capture.")],
        method: Annotated[
            CaptureMethod | None,
            Field(
                description=(
                    "Core-producing capture method (KDUMP/HOST_DUMP) the bound provider must "
                    "advertise. Omit to resolve the System profile's method; a profile with no "
                    "implicit core method requires an explicit one."
                )
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Capture and persist a vmcore."""
        return await handlers.fetch_vmcore(
            pool,
            current_context(),
            system_id=system_id,
            method=method,
            idempotency_key=idempotency_key,
        )

    @app.tool(
        name="vmcore.list",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Lists a System's redacted vmcore artifacts; those rows only exist after a "
                "live capture path runs, exercised under the gated live markers."
            ),
            promotion=(
                "A non-gated test or recorded live_stack run lists vmcore artifacts a real "
                "capture produced."
            ),
        ),
    )
    async def vmcore_list(
        system_id: Annotated[
            str,
            Field(description="The System whose redacted vmcore artifacts to list."),
        ],
    ) -> ToolResponse:
        """List vmcore artifacts for one system."""
        return await list_vmcores(pool, current_context(), system_id=system_id)

    @app.tool(
        name="postmortem.crash",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Runs allowlisted crash(8) verbs over a Run's captured core; requires a real "
                "captured vmcore, produced only under the gated live markers."
            ),
            promotion=(
                "A non-gated test or recorded live_stack run runs crash commands over a real "
                "captured core."
            ),
        ),
    )
    async def postmortem_crash_tool(
        run_id: Annotated[str, Field(description="The Run whose captured core to analyze.")],
        commands: Annotated[
            list[str],
            Field(description="Crash commands to run (allowlisted read-only verbs)."),
        ],
    ) -> ToolResponse:
        """Run crash postmortem commands for a captured vmcore."""
        return await handlers.postmortem_crash(
            pool, current_context(), run_id=run_id, commands=commands
        )

    @app.tool(
        name="postmortem.triage",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Runs the default crash triage batch over a Run's captured core; requires a "
                "real captured vmcore, produced only under the gated live markers."
            ),
            promotion=("A non-gated test or recorded live_stack run triages a real captured core."),
        ),
    )
    async def postmortem_triage_tool(
        run_id: Annotated[str, Field(description="The Run whose captured core to triage.")],
    ) -> ToolResponse:
        """Run the default crash triage for a captured vmcore."""
        return await handlers.postmortem_triage(pool, current_context(), run_id=run_id)
