"""Live drgn introspection handlers."""

from __future__ import annotations

import asyncio
import math
from typing import NamedTuple, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import LIVE_SCRIPT_MAX_TIMEOUT_SECONDS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.kernel_config.gate import debuginfo_warning
from kdive.log import bind_context
from kdive.mcp.responses import ResponseData, ToolResponse
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.debug.introspection.common import (
    _LIVE_INTROSPECTION,
    _LIVE_SCRIPT,
    _require_introspection,
)
from kdive.mcp.tools.debug.introspection.gate import augment_with_runtime_probe
from kdive.mcp.tools.debug.sessions.context import resolve_debug_session_context
from kdive.prereqs.system_bootstrap_key import (
    load_system_bootstrap_private_key,
    materialized_private_key,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.ports.lifecycle import DebugTransportKind, IntrospectionMode
from kdive.providers.ports.retrieve import LiveIntrospector, LiveScriptOutput
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.serialization import JsonValue

_LIVE_HELPERS = frozenset({"tasks", "modules", "sysinfo"})
_DRGN_LIVE: DebugTransportKind = "drgn-live"
# The agent-chosen ``introspect.script`` timeout is clamped to ``[_TIMEOUT_FLOOR, ceiling]`` before
# it drives the in-guest ``timeout drgn -k`` wrapper. The floor is > 0 because coreutils
# ``timeout 0`` means *no* timeout - a 0/negative value would delete the in-guest bound (ADR-0240).
_TIMEOUT_FLOOR = 1.0
_DEFAULT_SCRIPT_TIMEOUT = 30.0
# Bound the inbound script before send so an oversize script is a clean ``configuration_error``
# rather than an opaque guest-agent ``input-data`` rejection (ADR-0240); drgn scripts are tiny, so
# 256 KiB is generous and stays well under the qemu-guest-agent QMP input-data cap.
_MAX_SCRIPT_BYTES = 256 * 1024


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


class LiveDrgnContext(NamedTuple):
    """Resolved live drgn inputs after session/runtime/key gating."""

    session: LiveDrgnSession
    runtime: ProviderRuntime
    private_key: str


def _clamp_timeout(requested: float) -> float:
    """Clamp the agent timeout to ``[_TIMEOUT_FLOOR, operator-ceiling]`` before drgn."""
    ceiling = float(config.require(LIVE_SCRIPT_MAX_TIMEOUT_SECONDS))
    if not math.isfinite(requested) or requested < _TIMEOUT_FLOOR:
        requested = _TIMEOUT_FLOOR
    return min(requested, ceiling)


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


async def _resolve_live_introspection_context(
    *,
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    ctx: RequestContext,
    session_id: str,
    mode: IntrospectionMode,
    secret_registry: SecretRegistry,
) -> LiveDrgnContext | ToolResponse:
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
            # raise, so the handlers warn instead of reporting blind success (ADR-0322). The static
            # config check keys on the uploaded .config; a runtime probe covers the gap where BTF
            # is advertised but the guest drgn cannot actually load it (ADR-0329).
            has_vmlinux = resolved.debuginfo_ref is not None
            warning = await debuginfo_warning(
                conn, resolved.run_id, has_uploaded_vmlinux=has_vmlinux
            )
            warning = await augment_with_runtime_probe(
                warning,
                introspector=runtime.live_introspector,
                transport_handle=resolved.transport_handle,
                private_key=private_key,
                has_uploaded_vmlinux=has_vmlinux,
            )
            resolved = resolved._replace(missing_debuginfo=warning)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(session_id, exc)
    return LiveDrgnContext(resolved, runtime, private_key)


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


def _debuginfo_failure_data(
    missing_debuginfo: dict[str, JsonValue] | None,
) -> dict[str, JsonValue] | None:
    """The failure-response ``data`` carrying the debuginfo warning, or ``None`` when absent.

    A blind session (ADR-0329) can make the caller's helper/script exit non-zero, so the warning
    rides the error response too — the agent learns the debuginfo cause instead of an opaque attach
    failure.
    """
    if missing_debuginfo is None:
        return None
    return {"missing_debuginfo": missing_debuginfo}


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
    in-tree helpers - there is no caller-supplied drgn script. The port is the single redaction
    boundary, so the returned report is already masked; the raw drgn transcript is ``sensitive``
    and is never returned. Roots the SSH connection in the target System's bootstrap key.
    """
    with bind_context(principal=ctx.principal):
        resolved = await _resolve_live_introspection_context(
            pool=pool,
            resolver=resolver,
            ctx=ctx,
            session_id=session_id,
            mode=_LIVE_INTROSPECTION,
            secret_registry=secret_registry,
        )
        if isinstance(resolved, ToolResponse):
            return resolved
        return await _introspect_live_session(
            session_id,
            resolved=resolved.session,
            helper=helper,
            introspector=resolved.runtime.live_introspector,
            private_key=resolved.private_key,
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
        return ToolResponse.failure_from_error(
            response_id, exc, data=_debuginfo_failure_data(resolved.missing_debuginfo)
        )
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
    """Run a caller drgn script over a `live` drgn-live DebugSession; return capped stdout."""
    with bind_context(principal=ctx.principal):
        resolved = await _resolve_live_introspection_context(
            pool=pool,
            resolver=resolver,
            ctx=ctx,
            session_id=session_id,
            mode=_LIVE_SCRIPT,
            secret_registry=secret_registry,
        )
        if isinstance(resolved, ToolResponse):
            return resolved
        resp = await _run_live_script(
            session_id,
            resolved=resolved.session,
            script=script,
            timeout_sec=timeout_sec,
            introspector=resolved.runtime.live_introspector,
            private_key=resolved.private_key,
        )
        if resp.error_category is None:
            await _audit_introspect_script(pool, ctx, resolved.session, script)
        return resp


async def _audit_introspect_script(
    pool: AsyncConnectionPool, ctx: RequestContext, resolved: LiveDrgnSession, script: str
) -> None:
    """Attribute a successful introspect.script (arbitrary in-guest drgn exec) to the caller."""
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
    """Clamp the timeout, run the script off-loop, shape the response."""
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
        return ToolResponse.failure_from_error(
            response_id, exc, data=_debuginfo_failure_data(resolved.missing_debuginfo)
        )
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
