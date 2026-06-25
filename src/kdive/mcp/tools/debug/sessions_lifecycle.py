"""Debug session lifecycle handlers for the Connect plane (ADR-0032).

`debug.start_session(run_id, "gdbstub")` opens a single-attach gdbstub transport to the
Run's `ready` System and inserts a `debug_sessions` row `attach -> live` carrying the
transport handle + an initial heartbeat; `debug.end_session(session_id)` drives a live/attach
session `-> detached`. Both are **synchronous** (no JobKind): opening the transport is a
bounded RSP probe, not a long-running provider op.

Single-attach is per **System** (per gdbstub endpoint), joined through `runs.system_id` —
two Runs on one System share the one stub, so a second attach is `transport_conflict`. The
RSP probe runs **outside** the per-System advisory lock (it is multi-second network IO);
the conflict + System-ready checks are re-evaluated authoritatively under the lock before the
insert, and a lost race closes the just-opened transport (ADR-0032 §6a). The
`force_crash`/reboot `live -> detached` path is the control plane's
`_detach_sessions`); this module owns only the agent-initiated start/end.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, LiteralString, cast
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import DEBUG_SESSIONS, RUNS, SYSTEMS
from kdive.domain.capacity.state import DebugSessionState, RunState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import DebugSession, Run, System
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import ConfigErrorReason
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import capability_unsupported as _capability_unsupported
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools.debug.debug_session_telemetry import DebugSessionTelemetry
from kdive.mcp.tools.debug.ops import DebugEngineRuntime, DebugRuntimeResolver
from kdive.mcp.tools.debug.session_context import resolve_debug_session_context
from kdive.mcp.tools.lifecycle.vmcore import CONSOLE_CRASH_GUIDANCE
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProfilePolicy
from kdive.providers.ports import (
    DEBUG_TRANSPORT_KINDS,
    Connector,
    DebugTransportKind,
    SystemHandle,
    TransportHandle,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend

_GDBSTUB = "gdbstub"
_DRGN_LIVE = "drgn-live"
_log = logging.getLogger("kdive.mcp.tools.debug.sessions")
# An attach failure maps these provider categories onto the response envelope. A
# MISSING_DEPENDENCY (no live_vm host / unresolvable endpoint) surfaces as an attach failure:
# the agent cannot attach either way.
_ATTACH_FAILURE = frozenset({ErrorCategory.DEBUG_ATTACH_FAILURE, ErrorCategory.TRANSPORT_FAILURE})

# Author-controlled prose + next actions for the caller-actionable `debug.start_session`
# preconditions (#487, ADR-0142). `configuration_error` is not suppressed, so the `detail`
# reaches the caller alongside the existing `data` reason/`current_status` token. Fixed strings —
# no run state, guest output, or resource id is interpolated (no-leak seam, ADR-0123).
_NOT_BOOTED_DETAIL = "run is not booted; it must reach a successful boot before a live session"
_BOOT_FIRST_DETAIL = "run has no successful boot; boot it before starting a live session"
_CRASHED_HALTED_LIVE_DRGN_DETAIL = (
    "run crashed during early boot and is halted with a live gdbstub; attach over gdbstub. "
    "drgn-live needs a running in-guest sshd, which a halted crash does not have"
)

# A live/attach session occupies the System's single endpoint **for that transport kind**
# (single-attach per transport, ADR-0039 §4): a gdbstub and a drgn-live session may coexist on
# one System, but a second attach over the same transport is `transport_conflict`.
_OCCUPIED_SQL: LiteralString = (
    "SELECT 1 FROM debug_sessions s "
    "JOIN runs r ON r.id = s.run_id "
    "WHERE r.system_id = %s AND s.transport = %s AND s.state IN ('attach', 'live') LIMIT 1"
)


@dataclass(frozen=True)
class _AttachRequest:
    run: Run
    system: System
    session_id: UUID
    transport: DebugTransportKind
    connector: Connector


@dataclass(frozen=True)
class _DetachResources:
    connector: Connector
    runtime: DebugEngineRuntime | None = None


@dataclass(frozen=True)
class _AttachResources:
    connector: Connector
    profile_policy: ProfilePolicy
    supported_debug_transports: frozenset[DebugTransportKind]
    provider: str


type _ConnectorForRun = Callable[[AsyncConnection, Run], Awaitable[_AttachResources | ToolResponse]]
type _DetachResourcesForSession = Callable[
    [AsyncConnection, UUID], Awaitable[_DetachResources | ToolResponse]
]
type _InsertSession = Callable[
    [AsyncConnection, RequestContext, _AttachRequest, TransportHandle], Awaitable[ToolResponse]
]


def _resolved_connector_for_run(resolver: ProviderResolver) -> _ConnectorForRun:
    async def connector_for_run(conn: AsyncConnection, run: Run) -> _AttachResources | ToolResponse:
        try:
            runtime = await resolver.runtime_for_run(conn, run.id)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(str(run.id), exc)
        return _AttachResources(
            connector=runtime.connector,
            profile_policy=runtime.profile_policy,
            supported_debug_transports=runtime.supported_debug_transports,
            provider=runtime.component_sources.provider,
        )

    return connector_for_run


def _resolved_detach_resources(
    resolver: ProviderResolver, runtime_resolver: DebugRuntimeResolver | None
) -> _DetachResourcesForSession:
    async def detach_resources(
        conn: AsyncConnection, session_id: UUID
    ) -> _DetachResources | ToolResponse:
        try:
            binding = await resolver.binding_for_session(conn, session_id)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(str(session_id), exc)
        runtime: DebugEngineRuntime | None
        if runtime_resolver is None:
            runtime = None
        else:
            resolved_runtime = runtime_resolver.runtime_for_binding(
                binding, object_id=str(session_id)
            )
            if isinstance(resolved_runtime, ToolResponse):
                return resolved_runtime
            runtime = resolved_runtime
        return _DetachResources(connector=binding.runtime.connector, runtime=runtime)

    return detach_resources


def _secret_scope(session_id: UUID) -> str:
    return f"debug-session:{session_id}"


async def _system_for_run(conn: AsyncConnection, run: Run) -> System | None:
    return await SYSTEMS.get(conn, run.require_system_id())


async def _succeeded_boot_result(conn: AsyncConnection, run_id: UUID) -> dict[str, Any] | None:
    """Return the succeeded ``boot`` step result for ``run_id`` when one exists."""
    query: LiteralString = (
        "SELECT result FROM run_steps WHERE run_id = %s AND step = 'boot' AND state = 'succeeded'"
    )
    async with conn.cursor() as cur:
        await cur.execute(query, (run_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    result = row[0]
    return result if isinstance(result, dict) else {}


async def _system_occupied(
    conn: AsyncConnection, system_id: UUID, transport: DebugTransportKind
) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(_OCCUPIED_SQL, (system_id, transport))
        return await cur.fetchone() is not None


async def _open_transport(
    connector: Connector, system: System, transport: DebugTransportKind
) -> TransportHandle | ToolResponse:
    """Open the transport outside any lock; map a provider failure to an envelope."""
    handle_name = system.domain_name or str(system.id)
    try:
        return await asyncio.to_thread(
            connector.open_transport, SystemHandle(handle_name), transport
        )
    except CategorizedError as exc:
        category = (
            exc.category
            if exc.category in _ATTACH_FAILURE
            else _map_attach_failure_category(exc.category)
        )
        return ToolResponse.failure_from_error(str(system.id), exc, category=category)


def _map_attach_failure_category(category: ErrorCategory) -> ErrorCategory:
    """Map a non-attach provider category onto the response taxonomy (MISSING_DEPENDENCY)."""
    if category is ErrorCategory.MISSING_DEPENDENCY:
        return ErrorCategory.DEBUG_ATTACH_FAILURE
    return category


def _release_failed_attach_secret(
    registry: SecretRegistry, secret_scope: str, result: ToolResponse | TransportHandle
) -> None:
    if isinstance(result, ToolResponse) and result.status != "live":
        registry.release(secret_scope)


class DebugSessionHandlers:
    """Bound debug session lifecycle handlers.

    The public methods take only MCP-facing inputs; provider and test seams are bound once
    at construction, matching the lifecycle handler pattern used by runs and systems.
    """

    def __init__(
        self,
        *,
        connector_for_run: _ConnectorForRun,
        detach_resources: _DetachResourcesForSession,
        insert_session_locked: _InsertSession | None = None,
        secret_backend_factory: Callable[[UUID], SecretBackend] | None = None,
        secret_registry: SecretRegistry,
        telemetry: DebugSessionTelemetry | None = None,
    ) -> None:
        self._connector_for_run = connector_for_run
        self._detach_resources = detach_resources
        self._insert_session_locked = (
            _insert_session_locked if insert_session_locked is None else insert_session_locked
        )
        self._secret_backend_factory = secret_backend_factory
        self._secret_registry = secret_registry
        self._telemetry = telemetry or DebugSessionTelemetry.disabled()

    @classmethod
    def from_resolver(
        cls,
        resolver: ProviderResolver,
        *,
        runtime_resolver: DebugRuntimeResolver | None,
        insert_session_locked: _InsertSession | None = None,
        secret_backend_factory: Callable[[UUID], SecretBackend] | None = None,
        secret_registry: SecretRegistry,
        telemetry: DebugSessionTelemetry | None = None,
    ) -> DebugSessionHandlers:
        return cls(
            connector_for_run=_resolved_connector_for_run(resolver),
            detach_resources=_resolved_detach_resources(resolver, runtime_resolver),
            insert_session_locked=insert_session_locked,
            secret_backend_factory=secret_backend_factory,
            secret_registry=secret_registry,
            telemetry=telemetry,
        )

    async def start_session(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        run_id: str,
        transport: str = _GDBSTUB,
    ) -> ToolResponse:
        """Open a single-attach transport and insert a `live` DebugSession (contributor).

        For a ``transport="drgn-live"`` session whose profile realizes it over SSH (the
        local-libvirt section; ``drgn_live_requires_credential``) the guest credential is
        resolved from the profile's ``ssh_credential_ref`` through the bound secret backend
        factory **before** the transport is opened, so the redaction registry is seeded before
        any transport output can carry the value (ADR-0039 §2). gdbstub, and a guest-agent
        drgn-live realization (remote), need no credential.
        """
        uid = _as_uuid(run_id)
        if uid is None:
            return _invalid_uuid_error("run_id", run_id)
        if transport not in DEBUG_TRANSPORT_KINDS:
            return _config_error_reason(
                run_id,
                ConfigErrorReason.INVALID_TRANSPORT,
                accepted_values=sorted(DEBUG_TRANSPORT_KINDS),
                detail=f"transport {transport!r} is not a supported debug transport",
            )
        transport_kind = cast(DebugTransportKind, transport)
        session_id = uuid4()
        secret_scope = _secret_scope(session_id)
        with bind_context(principal=ctx.principal):
            async with pool.connection() as conn:
                request = await self._prepare_attach_request(
                    conn, ctx, uid, transport_kind, session_id
                )
            if isinstance(request, ToolResponse):
                return request
            opened = await _open_transport(request.connector, request.system, request.transport)
            _release_failed_attach_secret(self._secret_registry, secret_scope, opened)
            if isinstance(opened, ToolResponse):
                return opened
            async with pool.connection() as conn:
                try:
                    response = await self._insert_session_locked(
                        conn,
                        ctx,
                        request,
                        opened,
                    )
                except Exception:
                    await _close(request.connector, str(opened))
                    self._secret_registry.release(secret_scope)
                    raise
            _release_failed_attach_secret(self._secret_registry, secret_scope, response)
            return response

    async def _prepare_attach_request(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        run_id: UUID,
        transport: DebugTransportKind,
        session_id: UUID,
    ) -> _AttachRequest | ToolResponse:
        run = await RUNS.get(conn, run_id)
        if run is None or run.project not in ctx.projects:
            return _config_error(str(run_id))
        require_role(ctx, run.project, Role.CONTRIBUTOR)
        system = await _attach_preconditions(conn, run, transport)
        if isinstance(system, ToolResponse):
            return system
        resources = await self._connector_for_run(conn, run)
        if isinstance(resources, ToolResponse):
            return resources
        if transport not in resources.supported_debug_transports:
            return _capability_unsupported(
                str(run_id),
                capability=f"debug_transport:{transport}",
                provider=resources.provider,
                supported=sorted(resources.supported_debug_transports),
            )
        backend = self._credential_backend(session_id, transport)
        resolved = _resolve_credential(system, transport, resources.profile_policy, backend)
        if isinstance(resolved, ToolResponse):
            return resolved
        return _AttachRequest(
            run=run,
            system=system,
            session_id=session_id,
            transport=transport,
            connector=resources.connector,
        )

    def _credential_backend(
        self, session_id: UUID, transport: DebugTransportKind
    ) -> SecretBackend | None:
        if transport != _DRGN_LIVE or self._secret_backend_factory is None:
            return None
        return self._secret_backend_factory(session_id)

    async def end_session(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        session_id: str,
    ) -> ToolResponse:
        """Drive a live/attach DebugSession `-> detached` (idempotent on detached; contributor).

        Also reaps the lazy gdb-MI engine (ADR-0034 §4d): under the per-session lock it exits
        the gdb subprocess and drops the registry entry, so an ended session never strands a
        subprocess or holds the single-attach stub. Reaping a session that never ran a
        Debug-plane op is a no-op.
        """
        uid = _as_uuid(session_id)
        if uid is None:
            return _invalid_uuid_error("session_id", session_id)
        with bind_context(principal=ctx.principal):
            resources: _DetachResources
            async with pool.connection() as conn:
                resolved_session = await resolve_debug_session_context(
                    conn, ctx, session_id, include_system=True
                )
                if isinstance(resolved_session, ToolResponse):
                    return resolved_session
                if resolved_session.system_id is None:
                    return _config_error(
                        session_id,
                        detail="debug session has no associated System to detach from",
                    )
                resources_or_response = await self._detach_resources(conn, uid)
                if isinstance(resources_or_response, ToolResponse):
                    return resources_or_response
                resources = resources_or_response
                envelope = await _detach_locked(
                    conn, ctx, uid, resolved_session.system_id, resources.connector
                )
            if resources.runtime is not None:
                async with resources.runtime.lock_for(session_id):
                    resources.runtime.reap(session_id)
            self._secret_registry.release(_secret_scope(uid))
            seconds = (datetime.now(UTC) - resolved_session.session.created_at).total_seconds()
            outcome = "ok" if envelope.status != "error" else "error"
            self._telemetry.record(resolved_session.session.transport, outcome, seconds)
            return envelope


def _resolve_credential(
    system: System,
    transport: DebugTransportKind,
    profile_policy: ProfilePolicy,
    secret_backend: SecretBackend | None,
) -> None | ToolResponse:
    """Resolve + register the SSH credential before transport use (ADR-0039 §2 ordering).

    A credential is needed only for a ``drgn-live`` transport whose profile realizes it over
    SSH — the local-libvirt section, per ``ProfilePolicy.drgn_live_requires_credential``
    (ADR-0085 Decision 2). Returns ``None`` when none is required (gdbstub, or a guest-agent
    realization such as
    remote) or resolution succeeded, or a failure envelope. The resolved value is registered
    into the redaction registry by the backend (a structural post-condition of
    ``FileRefBackend.resolve``) before this returns — so the connector that opens the SSH
    connection runs with the registry already seeded.
    """
    if transport != _DRGN_LIVE:
        return None
    try:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(str(system.id), exc)
    if not profile_policy.drgn_live_requires_credential(profile):
        return None
    ref = profile_policy.ssh_credential_ref(profile)
    if ref is None:
        return _config_error(
            str(system.id),
            detail=(
                "System not provisioned for drgn-live; reprovision with "
                "`provider.local-libvirt.ssh_credential_ref` set — see the "
                "local-libvirt walkthrough."
            ),
            data={"reason": "ssh_credential_ref_missing"},
        )
    if secret_backend is None:
        return ToolResponse.failure(str(system.id), ErrorCategory.MISSING_DEPENDENCY)
    try:
        secret_backend.resolve(ref)
    except PathSafetyError:
        # A reference that escapes the secrets root / points at a non-file is a caller-config
        # error for the file-backed secret backend.
        return ToolResponse.failure(str(system.id), ErrorCategory.CONFIGURATION_ERROR)
    except CategorizedError as exc:
        # Preserve the backend's own category (e.g. a manager backend's MISSING_DEPENDENCY /
        # INFRASTRUCTURE_FAILURE) so a degraded secret store is not mislabeled as bad input.
        return ToolResponse.failure_from_error(str(system.id), exc)
    return None


async def _attach_preconditions(
    conn: AsyncConnection, run: Run, transport: DebugTransportKind
) -> System | ToolResponse:
    """Lockless pre-checks: Run booted, System present + `ready`, endpoint free.

    Returns the System on success, or a failure envelope. These are advisory fast-fails;
    `_insert_session_locked` re-checks conflict + ready authoritatively under the lock. The
    conflict check is scoped to ``transport`` (per-transport single-attach, ADR-0039 §4).
    """
    if run.state is not RunState.SUCCEEDED:
        return ToolResponse.failure(
            str(run.id),
            ErrorCategory.CONFIGURATION_ERROR,
            detail=_NOT_BOOTED_DETAIL,
            suggested_next_actions=["runs.get"],
            data={"current_status": run.state.value},
        )
    boot_result = await _succeeded_boot_result(conn, run.id)
    if boot_result is None:
        return ToolResponse.failure(
            str(run.id),
            ErrorCategory.CONFIGURATION_ERROR,
            detail=_BOOT_FIRST_DETAIL,
            suggested_next_actions=["runs.boot", "runs.get"],
            data={"reason": "boot_first"},
        )
    if boot_result.get("boot_outcome") == "expected_crash_observed":
        # An expected console_crash leaves the System READY, so vmcore.fetch always rejects and
        # postmortem.triage only self-corrects back to the console (#759). Point straight at the
        # console artifact and reuse postmortem.triage's shared CONSOLE_CRASH_GUIDANCE so the two
        # surfaces cannot drift.
        return ToolResponse.failure(
            str(run.id),
            ErrorCategory.CONFIGURATION_ERROR,
            detail=CONSOLE_CRASH_GUIDANCE,
            suggested_next_actions=["runs.get", "artifacts.list"],
            data={"reason": "expected_crash_not_live_debuggable"},
        )
    if boot_result.get("boot_outcome") == "crashed_halted_live" and transport == _DRGN_LIVE:
        return ToolResponse.failure(
            str(run.id),
            ErrorCategory.CONFIGURATION_ERROR,
            detail=_CRASHED_HALTED_LIVE_DRGN_DETAIL,
            suggested_next_actions=["debug.start_session"],
            data={"reason": "crashed_not_ssh_debuggable"},
        )
    # A `crashed_halted_live` outcome over gdbstub falls through to the System-ready/occupied
    # checks and is admitted (ADR-0233, #747) — do not re-add a blanket crash reject here.
    system = await _system_for_run(conn, run)
    if system is None:
        return _config_error(str(run.id))
    if system.state is not SystemState.READY:
        return _config_error(str(run.id), data={"current_status": system.state.value})
    if await _system_occupied(conn, system.id, transport):
        return ToolResponse.failure(str(run.id), ErrorCategory.TRANSPORT_CONFLICT)
    return system


async def _insert_session_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    request: _AttachRequest,
    handle: TransportHandle,
) -> ToolResponse:
    """Re-check conflict + ready under the per-System lock, then insert + drive `-> live`.

    A lost race (System crashed, or another attach committed first) closes the just-opened
    transport and returns the categorized error — no `live` row escapes the lock. The
    conflict re-check is scoped to ``transport`` (per-transport single-attach, ADR-0039 §4).
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, request.system.id):
        current = await SYSTEMS.get(conn, request.system.id)
        if current is None or current.state is not SystemState.READY:
            await _close(request.connector, str(handle))
            status = current.state.value if current else "torn_down"
            return _config_error(str(request.run.id), data={"current_status": status})
        if await _system_occupied(conn, request.system.id, request.transport):
            await _close(request.connector, str(handle))
            return ToolResponse.failure(str(request.run.id), ErrorCategory.TRANSPORT_CONFLICT)
        now = datetime.now(UTC)  # placeholder; the DB owns created_at/updated_at
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=request.session_id,
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=request.run.project,
                run_id=request.run.id,
                state=DebugSessionState.ATTACH,
                transport=request.transport,
                transport_handle=str(handle),
                worker_heartbeat_at=now,
            ),
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="debug.start_session",
                object_kind="debug_sessions",
                object_id=session.id,
                transition="->attach",
                args={"run_id": str(request.run.id)},
                project=request.run.project,
            ),
        )
        await DEBUG_SESSIONS.update_state(conn, session.id, DebugSessionState.LIVE)
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="debug.start_session",
                object_kind="debug_sessions",
                object_id=session.id,
                transition="attach->live",
                args={"run_id": str(request.run.id)},
                project=request.run.project,
            ),
        )
    return ToolResponse.success(
        str(session.id),
        "live",
        suggested_next_actions=["debug.end_session"],
        data={"project": request.run.project},
    )


async def _detach_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    session_id: UUID,
    system_id: UUID,
    connector: Connector,
) -> ToolResponse:
    select_q: LiteralString = (
        "SELECT state, transport_handle, project FROM debug_sessions WHERE id = %s FOR UPDATE"
    )
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(select_q, (session_id,))
            row = await cur.fetchone()
        if row is None:
            return _config_error(str(session_id))
        try:
            state = DebugSessionState(row["state"])
        except ValueError as exc:
            raise CategorizedError(
                f"debug session has an unrecognized state {row['state']!r}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"session_id": str(session_id)},
            ) from exc
        if state is DebugSessionState.DETACHED:
            return _detached_envelope(session_id, row["project"])
        await _close(connector, row["transport_handle"])
        await DEBUG_SESSIONS.update_state(conn, session_id, DebugSessionState.DETACHED)
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="debug.end_session",
                object_kind="debug_sessions",
                object_id=session_id,
                transition=f"{row['state']}->detached",
                args={"session_id": str(session_id)},
                project=row["project"],
            ),
        )
    return _detached_envelope(session_id, row["project"])


async def _close(connector: Connector, handle: str | None) -> None:
    """Close the transport best-effort; a missing/failing close never blocks the detach."""
    if handle is None:
        return
    try:
        await asyncio.to_thread(connector.close_transport, TransportHandle(handle))
    except Exception:
        _log.warning(
            "debug transport close failed; continuing detach",
            extra={"handle": handle},
            exc_info=True,
        )


def _detached_envelope(session_id: UUID, project: str) -> ToolResponse:
    return ToolResponse.success(
        str(session_id), "detached", suggested_next_actions=[], data={"project": project}
    )
