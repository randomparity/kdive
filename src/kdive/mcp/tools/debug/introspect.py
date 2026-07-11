"""The `introspect.from_vmcore` MCP tool: offline drgn introspection of a captured vmcore.

`introspect.from_vmcore(run_id)` is a synchronous offline viewer read (ADR-0033). It resolves
the Run's `debuginfo_ref` (the build-plane `vmlinux`), the build plane's recorded `build_id`
(provenance), and the Run's System's captured raw `vmcore` key through the shared
`mcp.tools._vmcore_targets` helper. It then runs the `VmcoreIntrospector` port and returns the
**already-redacted** report (the port is the single redaction boundary, ADR-0033 §6) as
structured data in `data["report"]`.

Real drgn is an operator-provided live-host prerequisite. Normal service startup leaves the
drgn-backed seams disabled; the live runner injects them only on hosts prepared for
``live_vm`` debugging.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Annotated, NamedTuple, cast
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.config.core_settings import LIVE_SCRIPT_MAX_TIMEOUT_SECONDS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.kernel_config.gate import debuginfo_warning
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ResponseData, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import capability_unsupported as _capability_unsupported
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run
from kdive.mcp.tools._vmcore_targets import resolve_run_vmcore_target, vmcore_target_failure
from kdive.mcp.tools.debug.session_context import resolve_debug_session_context
from kdive.prereqs.system_bootstrap_key import (
    load_system_bootstrap_private_key,
    materialized_private_key,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.ports.lifecycle import (
    DebugTransportKind,
    IntrospectionMode,
)
from kdive.providers.ports.retrieve import (
    LiveIntrospector,
    LiveScriptOutput,
    VmcoreIntrospector,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.serialization import JsonValue

# The fixed live-helper set (ADR-0033 §2 / ADR-0039 §3): the same three in-tree helpers as the
# offline path. There is no caller-supplied drgn script — an unknown helper is rejected.
_LIVE_HELPERS = frozenset({"tasks", "modules", "sysinfo"})
_DRGN_LIVE: DebugTransportKind = "drgn-live"
_OFFLINE_VMCORE: IntrospectionMode = "offline-vmcore"
_LIVE_INTROSPECTION: IntrospectionMode = "live"
_LIVE_SCRIPT: IntrospectionMode = "live-script"
# The agent-chosen ``introspect.script`` timeout is clamped to ``[_TIMEOUT_FLOOR, ceiling]`` before
# it drives the in-guest ``timeout drgn -k`` wrapper. The floor is > 0 because coreutils
# ``timeout 0`` means *no* timeout — a 0/negative value would delete the in-guest bound (ADR-0240).
_TIMEOUT_FLOOR = 1.0
_DEFAULT_SCRIPT_TIMEOUT = 30.0
# Bound the inbound script before send so an oversize script is a clean ``configuration_error``
# rather than an opaque guest-agent ``input-data`` rejection (ADR-0240); drgn scripts are tiny, so
# 256 KiB is generous and stays well under the qemu-guest-agent QMP input-data cap.
_MAX_SCRIPT_BYTES = 256 * 1024


def _clamp_timeout(requested: float) -> float:
    """Clamp the agent timeout to ``[_TIMEOUT_FLOOR, operator-ceiling]`` before it reaches drgn.

    A non-finite, ``0``, or negative request clamps up to the floor; an over-ceiling request clamps
    down to ``KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS``. The clamped value drives both the in-guest
    ``timeout`` and the derived transport timeout, so neither can be inflated past the ceiling.
    """
    ceiling = float(config.require(LIVE_SCRIPT_MAX_TIMEOUT_SECONDS))
    if not math.isfinite(requested) or requested < _TIMEOUT_FLOOR:
        requested = _TIMEOUT_FLOOR
    return min(requested, ceiling)


def _require_introspection(
    object_id: str, runtime: ProviderRuntime, mode: IntrospectionMode
) -> ToolResponse | None:
    """Reject an introspection mode the bound provider's descriptor lacks (ADR-0209).

    Returns a ``capability_unsupported`` ``configuration_error`` on a miss (no port is touched), or
    ``None`` when the provider advertises ``mode``. The check reads ``supported_introspection`` and
    never branches on ``ResourceKind``.
    """
    if mode in runtime.supported_introspection:
        return None
    return _capability_unsupported(
        object_id,
        capability=f"introspection:{mode}",
        provider=runtime.component_sources.provider,
        supported=sorted(runtime.supported_introspection),
    )


async def introspect_from_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    introspector: VmcoreIntrospector,
) -> ToolResponse:
    """Run offline drgn introspection over the Run's captured core; return the redacted report.

    Requires the viewer role. A malformed `run_id` is a `configuration_error`; a Run that is
    absent, in an ungranted project (no-leak), or missing its target artifact (no captured core,
    null `debuginfo_ref`, or no recorded `build` step — checked in that order, ADR-0165) is
    `not_found` (ADR-0097). A
    provenance mismatch or a drgn open/decode fault surfaces as the port's typed
    `CategorizedError` category, never a 500. Off a prepared live host, the provider seam reports
    ``missing_dependency`` instead of importing drgn.
    """
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            try:
                resolved = await resolve_run_vmcore_target(conn, ctx, run_id)
            except CategorizedError as exc:
                return vmcore_target_failure(run_id, exc)
        try:
            output = await asyncio.to_thread(
                introspector.from_vmcore,
                vmcore_ref=resolved.vmcore_ref,
                debuginfo_ref=resolved.debuginfo_ref,
                expected_build_id=resolved.build_id,
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(run_id, exc)
        report = {"tasks": output.tasks, "modules": output.modules, "sysinfo": output.sysinfo}
        return ToolResponse.success(
            run_id,
            "succeeded",
            suggested_next_actions=["introspect.from_vmcore", "artifacts.list"],
            data=cast(
                ResponseData,
                {"report": report, "truncated": output.truncated},
            ),
        )


class LiveDrgnSession(NamedTuple):
    """The resolved inputs needed to run live drgn introspection."""

    project: str
    transport_handle: str
    session_id: UUID
    system_id: UUID
    run_id: UUID
    # The owning Run's uploaded host vmlinux ref, if any (suppresses the ADR-0322 warning).
    debuginfo_ref: str | None = None
    # Non-fatal drgn-live debuginfo warning (ADR-0322); spread into the report when set.
    missing_debuginfo: dict[str, JsonValue] | None = None


type _LiveIntrospectionAction = Callable[
    [LiveDrgnSession, ProviderRuntime, str], Awaitable[ToolResponse]
]


async def resolve_live_drgn_session(
    conn: AsyncConnection, ctx: RequestContext, session_id: str
) -> LiveDrgnSession:
    """Resolve a `live` drgn-live DebugSession to the domain inputs required by the port.

    Gates on UUID shape, project scope, ``contributor`` role, ``live`` state, and the
    ``drgn-live`` transport (live introspection rides drgn-live, not gdbstub; ADR-0039 §4 /
    ADR-0085). The provider realizes drgn-live over SSH (local) or the guest agent (remote);
    core treats the resolved ``transport_handle`` as opaque. Also resolves the session's owning
    System id (via its Run) so the caller can load the System's SSH bootstrap key (ADR-0289).
    """
    resolved = await resolve_debug_session_context(
        conn,
        ctx,
        session_id,
        required_transport=_DRGN_LIVE,
        require_live=True,
        include_system=True,
    )
    if (
        isinstance(resolved, ToolResponse)
        or resolved.transport_handle is None
        or resolved.system_id is None
    ):
        raise _session_config_error()
    return LiveDrgnSession(
        resolved.project,
        resolved.transport_handle,
        resolved.session_id,
        resolved.system_id,
        resolved.session.run_id,
        resolved.debuginfo_ref,
    )


async def _with_live_introspection(
    *,
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    session_id: str,
    mode: IntrospectionMode,
    secret_registry: SecretRegistry,
    action: _LiveIntrospectionAction,
) -> ToolResponse:
    async with pool.connection() as conn:
        try:
            resolved = await resolve_live_drgn_session(conn, ctx, session_id)
            runtime = await resolver.runtime_for_session(conn, resolved.session_id)
            denied = _require_introspection(session_id, runtime, mode)
            if denied is not None:
                return denied
            private_key = await load_system_bootstrap_private_key(
                conn, resolved.system_id, secret_registry=secret_registry
            )
            # A live introspection over a debuginfo-less kernel resolves no symbols but does not
            # raise, so the handlers would report `succeeded` with blind output. This fail-open
            # read lets them warn (never refuse) — the `debuginfo_ref` was resolved with the
            # session, so no extra Run fetch is needed (ADR-0322).
            warning = await debuginfo_warning(
                conn, resolved.run_id, has_uploaded_vmlinux=resolved.debuginfo_ref is not None
            )
            resolved = resolved._replace(missing_debuginfo=warning)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(session_id, exc)
    return await action(resolved, runtime, private_key)


def _session_config_error() -> CategorizedError:
    return CategorizedError(
        "debug session does not resolve to a live drgn-live session",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


def _with_debuginfo_warning(
    data: ResponseData, missing_debuginfo: dict[str, JsonValue] | None
) -> ResponseData:
    """Add the non-fatal ``missing_debuginfo`` warning to a live-introspection report (ADR-0322)."""
    if missing_debuginfo is not None:
        data["missing_debuginfo"] = missing_debuginfo
    return data


async def introspect_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    session_id: str,
    helper: str,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> ToolResponse:
    """Run live drgn introspection over a `live` drgn-live DebugSession; return a redacted report.

    Requires a `live` drgn-live DebugSession (contributor). The ``helper`` must be one of the fixed
    in-tree helpers — there is no caller-supplied drgn script. The port is the single redaction
    boundary, so the returned report is already masked; the raw drgn transcript is ``sensitive``
    and is never returned (the response only advertises that, ADR-0039 §2/§3). Roots the SSH
    connection in the target System's per-System bootstrap key (ADR-0289), loaded and materialized
    to a caller-owned temp file removed on every exit path; a System with no bootstrap key row
    fails closed with ``CONFIGURATION_ERROR``.
    Off a prepared live host, the provider seam reports ``missing_dependency`` instead of
    importing drgn.
    """
    with bind_context(principal=ctx.principal):

        async def _run_live_helper(
            resolved: LiveDrgnSession, runtime: ProviderRuntime, private_key: str
        ) -> ToolResponse:
            return await _introspect_live_session(
                session_id,
                resolved=resolved,
                helper=helper,
                introspector=runtime.live_introspector,
                private_key=private_key,
            )

        return await _with_live_introspection(
            pool=pool,
            resolver=resolver,
            ctx=ctx,
            session_id=session_id,
            mode=_LIVE_INTROSPECTION,
            secret_registry=secret_registry,
            action=_run_live_helper,
        )


async def _introspect_live_session(
    response_id: str,
    *,
    resolved: LiveDrgnSession,
    helper: str,
    introspector: LiveIntrospector,
    private_key: str,
) -> ToolResponse:
    if helper not in _LIVE_HELPERS:
        return _config_error(response_id)
    try:
        with materialized_private_key(private_key) as key_path:
            output = await asyncio.to_thread(
                introspector.introspect_live,
                transport_handle=resolved.transport_handle,
                helper=helper,
                key_path=str(key_path),
            )
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(response_id, exc)
    sections = {"tasks": output.tasks, "modules": output.modules, "sysinfo": output.sysinfo}
    report = {helper: sections[helper]}
    data = cast(
        ResponseData,
        {
            "report": report,
            "truncated": output.truncated,
            "transcript_sensitivity": "sensitive",
        },
    )
    return ToolResponse.success(
        response_id,
        "succeeded",
        suggested_next_actions=["introspect.run", "debug.end_session"],
        data=_with_debuginfo_warning(data, resolved.missing_debuginfo),
    )


async def introspect_script(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    session_id: str,
    script: str,
    timeout_sec: float,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> ToolResponse:
    """Run a caller drgn script over a `live` drgn-live DebugSession; return capped stdout.

    Requires a `live` drgn-live DebugSession (contributor). ``timeout_sec`` is clamped to
    ``[1.0, ceiling]`` before it reaches the guest. The port runs the script **in the guest** and
    is the single redaction boundary, so the returned stdout is already masked of platform secrets
    and byte-capped; the raw transcript is ``sensitive`` and is never returned. Roots the SSH
    connection in the target System's per-System bootstrap key (ADR-0289), loaded and materialized
    to a caller-owned temp file removed on every exit path; a System with no bootstrap key row
    fails closed with ``CONFIGURATION_ERROR``. Off a prepared live host, the provider seam reports
    ``missing_dependency`` instead of importing drgn (ADR-0240).
    """
    with bind_context(principal=ctx.principal):

        async def _run_live_script_callback(
            resolved: LiveDrgnSession, runtime: ProviderRuntime, private_key: str
        ) -> ToolResponse:
            resp = await _run_live_script(
                session_id,
                resolved=resolved,
                script=script,
                timeout_sec=timeout_sec,
                introspector=runtime.live_introspector,
                private_key=private_key,
            )
            if resp.error_category is None:
                await _audit_introspect_script(pool, ctx, resolved, script)
            return resp

        return await _with_live_introspection(
            pool=pool,
            resolver=resolver,
            ctx=ctx,
            session_id=session_id,
            mode=_LIVE_SCRIPT,
            secret_registry=secret_registry,
            action=_run_live_script_callback,
        )


async def _audit_introspect_script(
    pool: AsyncConnectionPool, ctx: RequestContext, resolved: LiveDrgnSession, script: str
) -> None:
    """Attribute a successful introspect.script (arbitrary in-guest drgn exec) to the caller.

    Post-hoc (the script already ran in-guest): one audit_log row against the DebugSession, built
    from the session ``resolved`` inside the gate (not a second ``require_live`` resolve, which a
    concurrent ``end_session`` could turn into a silently-dropped row). The script text rides
    ``args`` for ``args_digest`` correlation only — hashed one-way, never stored plaintext.
    """
    async with pool.connection() as conn, conn.transaction():
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="introspect.script",
                object_kind="debug_sessions",
                object_id=resolved.session_id,
                transition="script",
                args={
                    "session_id": str(resolved.session_id),
                    "run_id": str(resolved.run_id),
                    "script": script,
                },
                project=resolved.project,
            ),
        )


async def _run_live_script(
    response_id: str,
    *,
    resolved: LiveDrgnSession,
    script: str,
    timeout_sec: float,
    introspector: LiveIntrospector,
    private_key: str,
) -> ToolResponse:
    """Clamp the timeout, run the script off-loop, shape the response (shared by tool + tests)."""
    script_bytes = len(script.encode("utf-8"))
    if script_bytes > _MAX_SCRIPT_BYTES:
        return _config_error(
            response_id,
            data={
                "reason": "script_too_large",
                "script_bytes": script_bytes,
                "max_bytes": _MAX_SCRIPT_BYTES,
            },
        )
    clamped = _clamp_timeout(timeout_sec)
    try:
        with materialized_private_key(private_key) as key_path:
            output: LiveScriptOutput = await asyncio.to_thread(
                introspector.run_script,
                transport_handle=resolved.transport_handle,
                script=script,
                timeout_sec=clamped,
                key_path=str(key_path),
            )
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(response_id, exc)
    data: ResponseData = {
        "output": output.output,
        "truncated": output.truncated,
        "transcript_sensitivity": "sensitive",
    }
    return ToolResponse.success(
        response_id,
        "succeeded",
        suggested_next_actions=["introspect.script", "debug.end_session"],
        data=_with_debuginfo_warning(data, resolved.missing_debuginfo),
    )


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `introspect.from_vmcore`, `introspect.run`, and `introspect.script` tools."""

    @app.tool(
        name="introspect.from_vmcore",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def introspect_from_vmcore_tool(
        run_id: Annotated[
            str,
            Field(
                description=(
                    "The Run whose captured core to introspect with operator-provided drgn."
                )
            ),
        ],
    ) -> ToolResponse:
        """Run offline drgn introspection over a Run's captured core; returns redacted report."""
        ctx = current_context()

        async def _gated(runtime: ProviderRuntime) -> ToolResponse:
            denied = _require_introspection(run_id, runtime, _OFFLINE_VMCORE)
            if denied is not None:
                return denied
            return await introspect_from_vmcore(
                pool, ctx, run_id=run_id, introspector=runtime.vmcore_introspector
            )

        return await with_runtime_for_run(
            pool, resolver, ctx, run_id, _gated, required_role=Role.VIEWER
        )

    @app.tool(
        name="introspect.run",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def introspect_run_tool(
        session_id: Annotated[str, Field(description="A live drgn-live DebugSession.")],
        helper: Annotated[
            str,
            Field(
                description=(
                    "In-tree drgn helper to run with operator-provided drgn: tasks, modules, "
                    "or sysinfo."
                )
            ),
        ],
    ) -> ToolResponse:
        """Run live drgn introspection over a live drgn-live DebugSession. Requires contributor."""
        return await introspect_run(
            pool,
            current_context(),
            session_id=session_id,
            helper=helper,
            resolver=resolver,
            secret_registry=secret_registry,
        )

    @app.tool(
        name="introspect.script",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def introspect_script_tool(
        session_id: Annotated[str, Field(description="A live drgn-live DebugSession.")],
        script: Annotated[
            str,
            Field(
                description=(
                    "A drgn (Python) script run against the live guest kernel; `prog` is the live "
                    "drgn.Program. Its stdout is returned (byte-capped). Each call is a fresh drgn "
                    "process — put any multi-step work in one script."
                )
            ),
        ],
        timeout_sec: Annotated[
            float,
            Field(
                description=(
                    f"In-guest execution bound (seconds); clamped to [{_TIMEOUT_FLOOR}, operator "
                    f"ceiling]. Defaults to {int(_DEFAULT_SCRIPT_TIMEOUT)}. A wedged script is "
                    "recovered with debug.end_session."
                )
            ),
        ] = _DEFAULT_SCRIPT_TIMEOUT,
    ) -> ToolResponse:
        """Run a caller drgn script over a live drgn-live DebugSession. Requires contributor."""
        return await introspect_script(
            pool,
            current_context(),
            session_id=session_id,
            script=script,
            timeout_sec=timeout_sec,
            resolver=resolver,
            secret_registry=secret_registry,
        )
