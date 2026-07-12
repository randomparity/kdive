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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, LiteralString, cast
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capacity.state import RunState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run, System
from kdive.domain.lifecycle.run_steps import (
    BOOT_OUTCOME_CRASHED_HALTED_LIVE,
    BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED,
    RUN_STEP_SUCCEEDED,
    parse_boot_outcome,
)
from kdive.kernel_config.gate import debuginfo_warning
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import ConfigErrorReason
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import capability_unsupported as _capability_unsupported
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools.debug.introspection.gate import augment_with_runtime_probe
from kdive.mcp.tools.debug.sessions.context import resolve_debug_session_context
from kdive.mcp.tools.lifecycle.vmcore.view import CONSOLE_CRASH_GUIDANCE
from kdive.observability.debug_session_telemetry import (
    DebugSessionOutcome,
    DebugSessionTelemetry,
)
from kdive.prereqs.system_bootstrap_key import load_system_bootstrap_private_key
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.handles import (
    SystemHandle,
    TransportHandle,
)
from kdive.providers.ports.lifecycle import (
    DEBUG_TRANSPORT_KINDS,
    Connector,
    DebugTransportKind,
)
from kdive.providers.ports.retrieve import LiveIntrospector
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.serialization import JsonValue
from kdive.services.debug import lifecycle as debug_lifecycle

if TYPE_CHECKING:
    from kdive.mcp.tools.debug.operations.runtime import DebugEngineRuntime, DebugRuntimeResolver

_GDBSTUB = "gdbstub"
_DRGN_LIVE = "drgn-live"
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
    live_introspector: LiveIntrospector


@dataclass(frozen=True, slots=True)
class _RuntimeProbe:
    """Inputs for the post-open drgn-live runtime debuginfo probe (ADR-0335).

    Present only when a runtime probe can still change the verdict: a drgn-live attach whose static
    config check is silent (BTF advertised, or no config) and whose Run uploaded no host vmlinux.
    The probe runs after the transport opens because the transport handle does not exist where the
    static warning is computed (:meth:`DebugSessionHandlers._prepare_attach_request`).
    """

    system_id: UUID
    introspector: LiveIntrospector


@dataclass(frozen=True, slots=True)
class _AttachPlan:
    """A prepared attach plus the optional post-open runtime probe (ADR-0335)."""

    request: debug_lifecycle.AttachRequest
    probe: _RuntimeProbe | None = None


type _ConnectorForRun = Callable[[AsyncConnection, Run], Awaitable[_AttachResources | ToolResponse]]
type _DetachResourcesForSession = Callable[
    [AsyncConnection, UUID], Awaitable[_DetachResources | ToolResponse]
]
type _InsertSession = Callable[
    [AsyncConnection, RequestContext, debug_lifecycle.AttachRequest, TransportHandle],
    Awaitable[ToolResponse],
]
type _PrepareAttachRequest = Callable[
    [AsyncConnection, RequestContext, UUID, DebugTransportKind, UUID],
    Awaitable[_AttachPlan | ToolResponse],
]
type _AugmentAfterOpen = Callable[
    [AsyncConnectionPool, _AttachPlan, TransportHandle],
    Awaitable[debug_lifecycle.AttachRequest],
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
            supported_debug_transports=runtime.support.debug_transports,
            provider=runtime.support.component_sources.provider,
            live_introspector=runtime.live_introspector,
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


async def _system_for_run(conn: AsyncConnection, run: Run) -> System | None:
    return await SYSTEMS.get(conn, run.require_system_id())


async def _succeeded_boot_result(conn: AsyncConnection, run_id: UUID) -> dict[str, Any] | None:
    """Return the succeeded ``boot`` step result for ``run_id`` when one exists."""
    query: LiteralString = (
        "SELECT result FROM run_steps WHERE run_id = %s AND step = 'boot' AND state = %s"
    )
    async with conn.cursor() as cur:
        await cur.execute(query, (run_id, RUN_STEP_SUCCEEDED))
        row = await cur.fetchone()
    if row is None:
        return None
    result = row[0]
    return result if isinstance(result, dict) else {}


async def _system_occupied(
    conn: AsyncConnection, system_id: UUID, transport: DebugTransportKind
) -> bool:
    return await debug_lifecycle.system_occupied(conn, system_id, transport)


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
        secret_registry: SecretRegistry,
        telemetry: DebugSessionTelemetry | None = None,
    ) -> None:
        self._connector_for_run = connector_for_run
        self._detach_resources = detach_resources
        self._insert_session_locked = insert_session_locked or _insert_session_locked
        self._secret_registry = secret_registry
        self._telemetry = telemetry or DebugSessionTelemetry.disabled()

    @classmethod
    def from_resolver(
        cls,
        resolver: ProviderResolver,
        *,
        runtime_resolver: DebugRuntimeResolver | None,
        insert_session_locked: _InsertSession | None = None,
        secret_registry: SecretRegistry,
        telemetry: DebugSessionTelemetry | None = None,
    ) -> DebugSessionHandlers:
        return cls(
            connector_for_run=_resolved_connector_for_run(resolver),
            detach_resources=_resolved_detach_resources(resolver, runtime_resolver),
            insert_session_locked=insert_session_locked,
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

        A ``transport="drgn-live"`` session whose profile realizes it over the loopback SSH forward
        (the local-libvirt section; ``ProfilePolicy.drgn_live_seeds_bootstrap_key``) fails closed if
        the target System has no per-System bootstrap key (ADR-0289), and seeds the redaction
        registry from that key **before** the transport is opened — so the registry is seeded before
        any transport output can carry it (ADR-0039 §2, ADR-0315). gdbstub, and a guest-agent
        drgn-live realization (remote), need no start-time seed.
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
        return await _attach_debug_session(
            pool,
            ctx,
            run_id=uid,
            transport=transport_kind,
            session_id=session_id,
            prepare_attach_request=self._prepare_attach_request,
            augment_after_open=self._augment_after_open,
            insert_session_locked=self._insert_session_locked,
        )

    async def _prepare_attach_request(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        run_id: UUID,
        transport: DebugTransportKind,
        session_id: UUID,
    ) -> _AttachPlan | ToolResponse:
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
        seeded = await self._seed_bootstrap_key(conn, system, transport, resources.profile_policy)
        if isinstance(seeded, ToolResponse):
            return seeded
        missing = await self._debuginfo_warning(conn, run, transport)
        request = debug_lifecycle.AttachRequest(
            run=run,
            system=system,
            session_id=session_id,
            transport=transport,
            connector=resources.connector,
            missing_debuginfo=missing,
        )
        probe = _runtime_probe(run, system, transport, missing, resources.live_introspector)
        return _AttachPlan(request=request, probe=probe)

    async def _augment_after_open(
        self,
        pool: AsyncConnectionPool,
        plan: _AttachPlan,
        handle: TransportHandle,
    ) -> debug_lifecycle.AttachRequest:
        """Fold the runtime debuginfo probe into the attach warning once the transport is open.

        The static warning is computed before the transport exists; ADR-0335 recomputes it here,
        over the just-opened attach transport, so ``debug.start_session(drgn-live)`` on a blind
        guest carries a ``debuginfo_unloadable`` warning **before** any ``introspect`` call. Runs
        outside the per-System lock (like ``_open_transport``) and is fully fail-open — a missing
        bootstrap key or an indeterminate probe leaves the static warning unchanged and never blocks
        the attach.
        """
        probe = plan.probe
        if probe is None:
            return plan.request
        private_key = await self._probe_private_key(pool, probe.system_id)
        if private_key is None:
            return plan.request
        warning = await augment_with_runtime_probe(
            plan.request.missing_debuginfo,
            introspector=probe.introspector,
            transport_handle=str(handle),
            private_key=private_key,
            has_uploaded_vmlinux=False,
        )
        return replace(plan.request, missing_debuginfo=warning)

    async def _probe_private_key(self, pool: AsyncConnectionPool, system_id: UUID) -> str | None:
        """Load the per-System bootstrap key for the runtime probe; fail open (``None``) if absent.

        A drgn-live realization that does not ride the loopback SSH forward (guest-agent) never
        seeds a key, so its absence is expected: the probe is skipped and the static warning stands.
        """
        try:
            async with pool.connection() as conn:
                return await load_system_bootstrap_private_key(
                    conn, system_id, secret_registry=self._secret_registry
                )
        except CategorizedError:
            return None

    async def _debuginfo_warning(
        self, conn: AsyncConnection, run: Run, transport: DebugTransportKind
    ) -> dict[str, JsonValue] | None:
        """Compute the non-fatal drgn-live ``missing_debuginfo`` warning (ADR-0322), else ``None``.

        Only drgn-live resolves symbols from the in-guest kernel, so gdbstub (which symbolizes from
        the host-side uploaded ``vmlinux``) never warns here. Runs outside the per-System lock — its
        fail-open config read never blocks the attach.
        """
        if transport != _DRGN_LIVE:
            return None
        return await debuginfo_warning(
            conn, run.id, has_uploaded_vmlinux=run.debuginfo_ref is not None
        )

    async def _seed_bootstrap_key(
        self,
        conn: AsyncConnection,
        system: System,
        transport: DebugTransportKind,
        profile_policy: ProfilePolicy,
    ) -> None | ToolResponse:
        """Gate + seed drgn-live on the per-System bootstrap key before the transport opens.

        For a drgn-live transport whose profile realizes it over the loopback SSH forward
        (``drgn_live_seeds_bootstrap_key``), load the System's per-System bootstrap key: the loader
        fails closed with ``configuration_error`` (``reason="no_bootstrap_key"``) when absent and
        registers the key value into the redaction registry, so the connector runs with the registry
        already seeded (ADR-0289, ADR-0039 §2, ADR-0315). Returns ``None`` when no seed is needed
        (gdbstub, or a guest-agent drgn-live realization) or the seed succeeded, or a failure
        envelope. Parsing the stored profile here also surfaces a retired-field configuration error
        as ``configuration_error`` rather than letting it escape.
        """
        if transport != _DRGN_LIVE:
            return None
        try:
            profile = ProvisioningProfile.parse(system.provisioning_profile)
            if profile_policy.drgn_live_seeds_bootstrap_key(profile):
                await load_system_bootstrap_private_key(
                    conn, system.id, secret_registry=self._secret_registry
                )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(str(system.id), exc)
        return None

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
        return await _end_debug_session(
            pool,
            ctx,
            session_id=session_id,
            uid=uid,
            detach_resources=self._detach_resources,
            telemetry=self._telemetry,
        )


def _runtime_probe(
    run: Run,
    system: System,
    transport: DebugTransportKind,
    static_warning: dict[str, JsonValue] | None,
    introspector: LiveIntrospector,
) -> _RuntimeProbe | None:
    """Build the post-open runtime probe only when it can still change the verdict (ADR-0335).

    Confined to the gap the static config check cannot cover: a drgn-live attach whose static
    warning is silent (BTF advertised, or no config) and whose Run uploaded no host vmlinux. gdbstub
    (symbolizes host-side), a Run that already warns, or an uploaded vmlinux pay nothing new.
    """
    if transport != _DRGN_LIVE or static_warning is not None or run.debuginfo_ref is not None:
        return None
    return _RuntimeProbe(system_id=system.id, introspector=introspector)


async def _attach_debug_session(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: UUID,
    transport: DebugTransportKind,
    session_id: UUID,
    prepare_attach_request: _PrepareAttachRequest,
    augment_after_open: _AugmentAfterOpen,
    insert_session_locked: _InsertSession,
) -> ToolResponse:
    """Prepare, open, augment the debuginfo warning, and persist one debug transport attach."""
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            plan = await prepare_attach_request(conn, ctx, run_id, transport, session_id)
        if isinstance(plan, ToolResponse):
            return plan
        opened = await _open_transport(
            plan.request.connector, plan.request.system, plan.request.transport
        )
        if isinstance(opened, ToolResponse):
            return opened
        request = await augment_after_open(pool, plan, opened)
        async with pool.connection() as conn:
            try:
                return await insert_session_locked(conn, ctx, request, opened)
            except Exception:
                await _close(request.connector, str(opened))
                raise


async def _end_debug_session(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    session_id: str,
    uid: UUID,
    detach_resources: _DetachResourcesForSession,
    telemetry: DebugSessionTelemetry,
) -> ToolResponse:
    """Resolve, detach, reap runtime state, and record telemetry for one debug session."""
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
            resources_or_response = await detach_resources(conn, uid)
            if isinstance(resources_or_response, ToolResponse):
                return resources_or_response
            resources = resources_or_response
            envelope = await _detach_locked(
                conn, ctx, uid, resolved_session.system_id, resources.connector
            )
        if resources.runtime is not None:
            async with resources.runtime.lock_for(session_id):
                resources.runtime.reap(session_id)
        seconds = (datetime.now(UTC) - resolved_session.session.created_at).total_seconds()
        outcome: DebugSessionOutcome = "ok" if envelope.status != "error" else "error"
        telemetry.record(resolved_session.session.transport, outcome, seconds)
        return envelope


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
    boot_outcome = parse_boot_outcome(boot_result.get("boot_outcome"))
    if boot_outcome == BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED:
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
    if boot_outcome == BOOT_OUTCOME_CRASHED_HALTED_LIVE and transport == _DRGN_LIVE:
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
    request: debug_lifecycle.AttachRequest,
    handle: TransportHandle,
) -> ToolResponse:
    """Persist an admitted session and render the transport-neutral service result."""
    result = await debug_lifecycle.insert_session_locked(conn, ctx, request, handle)
    return _render_attach_result(result, transport=request.transport)


async def _detach_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    session_id: UUID,
    system_id: UUID,
    connector: Connector,
) -> ToolResponse:
    result = await debug_lifecycle.detach_locked(conn, ctx, session_id, system_id, connector)
    return _render_detach_result(result)


async def _close(connector: Connector, handle: str | None) -> None:
    """Close the transport best-effort; a missing/failing close never blocks the detach."""
    await debug_lifecycle.close_transport(connector, handle)


def _render_attach_result(
    result: debug_lifecycle.AttachAdmitted | debug_lifecycle.DebugSessionRejected,
    *,
    transport: DebugTransportKind,
) -> ToolResponse:
    if isinstance(result, debug_lifecycle.DebugSessionRejected):
        return _render_rejection(result)
    data: dict[str, JsonValue] = {"project": result.project}
    actions = []
    if result.missing_debuginfo is not None:
        data["missing_debuginfo"] = result.missing_debuginfo
        actions.append("artifacts.feature_config_requirements")
    if transport == _DRGN_LIVE:
        actions.extend(["introspect.run", "introspect.script"])
    actions.append("debug.end_session")
    return ToolResponse.success(
        str(result.session_id),
        "live",
        suggested_next_actions=actions,
        data=data,
    )


def _render_detach_result(
    result: debug_lifecycle.DetachedSession | debug_lifecycle.DebugSessionRejected,
) -> ToolResponse:
    if isinstance(result, debug_lifecycle.DebugSessionRejected):
        return _render_rejection(result)
    return _detached_envelope(result.session_id, result.project)


def _render_rejection(result: debug_lifecycle.DebugSessionRejected) -> ToolResponse:
    return ToolResponse.failure(
        result.object_id,
        result.category,
        detail=result.detail,
        suggested_next_actions=result.suggested_next_actions,
        data=result.data,
    )


def _detached_envelope(session_id: UUID, project: str) -> ToolResponse:
    return ToolResponse.success(
        str(session_id), "detached", suggested_next_actions=[], data={"project": project}
    )
