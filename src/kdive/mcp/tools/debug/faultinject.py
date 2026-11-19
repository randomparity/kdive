"""``faultinject.inspect``, ``set`` and ``reset``: guest fault-injection knobs (ADR-0825, 0826)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ResponseData, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import capability_unsupported as _capability_unsupported
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools.lifecycle.systems.ssh_access import recorded_endpoint
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.fault_injection import (
    ARMING_BOUNDS,
    READ_WORST_CASE_SECONDS,
    REPLY_BYTE_CAP,
    FaultInjectionInspector,
    FaultKnobState,
    validate_knob,
    validate_set_request,
)
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.system_bootstrap_key import (
    load_system_bootstrap_private_key,
    materialized_private_key,
)

_NOT_READY_DETAIL = "System is not ready; fault-injection state is readable only on a ready System."
_UNPROVISIONED_DETAIL = (
    "This System records no SSH forward, so the guest cannot be read; reprovision a System "
    "created before the forward was rendered."
)
_CAPABILITY = "fault_injection"
_SUPPORTED_PROVIDERS = ["local-libvirt"]


@dataclass(frozen=True, slots=True)
class _Guest:
    system: System
    inspector: FaultInjectionInspector
    handle: str
    private_key: str


async def _resolve_guest(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    role: Role,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> ToolResponse | _Guest:
    """Authorize ``role`` on a ready System and gather what one helper round trip needs."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            require_role(ctx, system.project, role)
            if system.state is not SystemState.READY:
                return ToolResponse.failure(
                    system_id, ErrorCategory.READINESS_FAILURE, detail=_NOT_READY_DETAIL
                )
            try:
                runtime = (await resolver.binding_for_system(conn, uid)).runtime
                inspector = runtime.fault_injection
                if inspector is None:
                    return _capability_unsupported(
                        system_id,
                        capability=_CAPABILITY,
                        provider=runtime.support.component_sources.provider,
                        supported=_SUPPORTED_PROVIDERS,
                    )
                recorded, _forward = await recorded_endpoint(conn, resolver, system)
                if recorded is None:
                    return ToolResponse.failure(
                        system_id,
                        ErrorCategory.CONFIGURATION_ERROR,
                        detail=_UNPROVISIONED_DETAIL,
                        data={"reason": "ssh_not_provisioned"},
                    )
                private_key = await load_system_bootstrap_private_key(
                    conn, uid, secret_registry=secret_registry
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(system_id, exc)
    # The pooled connection is released before the SSH round trip, which can take seconds.
    host, port = recorded
    handle = TransportHandleData(kind="ssh", host=host, port=port).encode()
    return _Guest(system, inspector, handle, private_key)


def _knobs_data(state: dict[str, FaultKnobState]) -> ResponseData:
    knobs: ResponseData = {}
    for name, knob in state.items():
        knobs[name] = {"present": knob.present, "armed": knob.armed, **knob.parameters}
    return {"knobs": knobs}


async def _run_port(
    system_id: str,
    guest: _Guest,
    call: Callable[[str, str], dict[str, FaultKnobState]],
) -> dict[str, FaultKnobState] | ToolResponse:
    """Run one port call off the event loop with the System's key materialized for it."""
    try:
        with materialized_private_key(guest.private_key) as key_path:
            return await asyncio.to_thread(call, guest.handle, str(key_path))
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(system_id, exc)


async def inspect_faults(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> ToolResponse:
    """Read each allowlisted knob's state from a ready local-libvirt System (viewer)."""
    guest = await _resolve_guest(
        pool, ctx, system_id, Role.VIEWER, resolver=resolver, secret_registry=secret_registry
    )
    if isinstance(guest, ToolResponse):
        return guest
    state = await _run_port(
        system_id,
        guest,
        lambda handle, key: guest.inspector.read_state(transport_handle=handle, key_path=key),
    )
    if isinstance(state, ToolResponse):
        return state
    data = _knobs_data(state)
    armed = any(knob.armed for knob in state.values())
    actions = ["faultinject.reset", "systems.get"] if armed else ["systems.get"]
    return ToolResponse.success(system_id, "ok", suggested_next_actions=actions, data=data)


async def set_fault(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    knob: str,
    parameters: Mapping[str, int],
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> ToolResponse:
    """Arm one knob with bounded parameters on a ready local-libvirt System (contributor)."""
    try:
        validate_set_request(knob, parameters)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(system_id, exc)
    guest = await _resolve_guest(
        pool, ctx, system_id, Role.CONTRIBUTOR, resolver=resolver, secret_registry=secret_registry
    )
    if isinstance(guest, ToolResponse):
        return guest
    state = await _run_port(
        system_id,
        guest,
        lambda handle, key: guest.inspector.set_knob(
            transport_handle=handle, key_path=key, knob=knob, parameters=parameters
        ),
    )
    args = {"knob": knob, **parameters}
    if isinstance(state, ToolResponse):
        return await _audit_unknown_outcome(pool, ctx, guest.system, "set", args, state)
    await _audit(pool, ctx, guest.system, "set", args)
    return ToolResponse.success(
        system_id,
        "ok",
        suggested_next_actions=["faultinject.inspect", "faultinject.reset"],
        data=_knobs_data(state),
    )


async def reset_faults(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    knob: str | None,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> ToolResponse:
    """Restore one knob, or every present knob, to the kernel defaults (contributor)."""
    try:
        if knob is not None:
            validate_knob(knob)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(system_id, exc)
    guest = await _resolve_guest(
        pool, ctx, system_id, Role.CONTRIBUTOR, resolver=resolver, secret_registry=secret_registry
    )
    if isinstance(guest, ToolResponse):
        return guest
    state = await _run_port(
        system_id,
        guest,
        lambda handle, key: guest.inspector.reset(transport_handle=handle, key_path=key, knob=knob),
    )
    if isinstance(state, ToolResponse):
        return await _audit_unknown_outcome(pool, ctx, guest.system, "reset", {"knob": knob}, state)
    await _audit(pool, ctx, guest.system, "reset", {"knob": knob})
    return ToolResponse.success(
        system_id, "ok", suggested_next_actions=["faultinject.inspect"], data=_knobs_data(state)
    )


_UNKNOWN_OUTCOME = frozenset(
    {ErrorCategory.TRANSPORT_FAILURE.value, ErrorCategory.INFRASTRUCTURE_FAILURE.value}
)


async def _audit_unknown_outcome(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system: System,
    transition: str,
    args: Mapping[str, object],
    failure: ToolResponse,
) -> ToolResponse:
    """Attribute a write that failed after it may have reached the guest, then return it.

    A transport or infrastructure failure leaves the guest state unknown, so the attempt is
    recorded; a configuration error means the helper wrote nothing and leaves no row.
    """
    if failure.error_category in _UNKNOWN_OUTCOME:
        await _audit(pool, ctx, system, transition, {**args, "outcome": "unknown"})
    return failure


async def _audit(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system: System,
    transition: str,
    args: Mapping[str, object],
) -> None:
    """Attribute a successful guest-state write to the caller."""
    async with pool.connection() as conn, conn.transaction():
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool=f"faultinject.{transition}",
                object_kind="systems",
                object_id=system.id,
                transition=transition,
                args=args,
                project=system.project,
            ),
        )


def _with_limits[F: Callable[..., object]](func: F) -> F:
    """Fill the docstring's numeric limits from the constants that enforce them."""
    if func.__doc__ is None:
        raise ValueError("faultinject.inspect needs a docstring")
    func.__doc__ = func.__doc__.format(
        byte_cap=REPLY_BYTE_CAP,
        seconds=READ_WORST_CASE_SECONDS,
        times_max=ARMING_BOUNDS["times"][1],
        space_max=ARMING_BOUNDS["space"][1],
        interval_max=ARMING_BOUNDS["interval"][1],
    )
    return func


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `faultinject.inspect`, `.set` and `.reset` tools."""

    @app.tool(
        name="faultinject.inspect",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    @_with_limits
    async def faultinject_inspect(
        system_id: Annotated[
            str,
            Field(
                description=(
                    "A ready local-libvirt System whose guest image carries the "
                    "kdive-faultinject helper and whose kernel enables fault injection."
                )
            ),
        ],
    ) -> ToolResponse:
        """Read the kernel fault-injection knobs of a ready local-libvirt System. Requires viewer.

        Reads failslab and fail_page_alloc through the guest's kdive-faultinject helper over the
        System's loopback SSH. Returns data.knobs, keyed by knob name. Each knob has present (bool,
        false when the kernel lacks it), armed (bool), and, when present, the current integers
        probability (percent, 0 to 100), interval, times (-1 is unlimited, 0 is exhausted), space,
        verbose, and for fail_page_alloc min-order. A knob is armed when probability is above 0 and
        times is not 0: it can then fail allocations in every task in the guest, including sshd.
        suggested_next_actions adds faultinject.reset while any knob is armed.

        The helper reply is capped at {byte_cap} bytes per call and is never truncated: an
        oversized or malformed reply is infrastructure_failure and retrying changes nothing. A
        call takes at most about {seconds} seconds. While a knob is armed the guest can fail this
        call itself (transport_failure or infrastructure_failure).

        Errors: not_found (unknown System or project); readiness_failure (System is not ready);
        configuration_error with data.reason capability_unsupported (provider is not
        local-libvirt), ssh_not_provisioned (no SSH forward), debugfs_not_mounted (mount debugfs in
        the guest), fault_injection_unsupported (neither knob exists: boot a kernel with
        CONFIG_FAULT_INJECTION, CONFIG_FAILSLAB and CONFIG_FAIL_PAGE_ALLOC) or helper_not_installed
        (rebuild the guest image with kdive-faultinject staged); transport_failure (ssh to the
        guest failed); infrastructure_failure (the helper failed or replied unexpectedly).
        """
        return await inspect_faults(
            pool,
            current_context(),
            system_id,
            resolver=resolver,
            secret_registry=secret_registry,
        )

    @app.tool(
        name="faultinject.set",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    @_with_limits
    async def faultinject_set(
        system_id: Annotated[
            str,
            Field(
                description=(
                    "A ready local-libvirt System whose guest image carries the "
                    "kdive-faultinject helper and whose kernel enables fault injection."
                )
            ),
        ],
        knob: Annotated[
            str,
            Field(
                description=(
                    "The knob to arm: failslab (fail kernel slab allocations) or "
                    "fail_page_alloc (fail page allocations). Any other name is rejected "
                    "(configuration_error, reason unknown_knob) before the guest is contacted."
                )
            ),
        ],
        probability: Annotated[
            int,
            Field(
                description=(
                    "Percent chance, 1 to 100, that each eligible allocation fails, per "
                    "allocation in every guest task. 0 is refused: arming needs a nonzero "
                    "chance (use faultinject.reset to disarm). Outside the range the call is "
                    "rejected with reason parameter_out_of_range; send a value in range."
                )
            ),
        ],
        times: Annotated[
            int,
            Field(
                description=(
                    "How many failures the guest kernel injects, 1 to {times_max}, counted "
                    "across all tasks in this guest; then it stops injecting by itself, with "
                    "no clock and no later call needed. -1 (unlimited) and 0 are refused with "
                    "reason parameter_out_of_range before the guest is contacted, so a guest "
                    "that can no longer run faultinject.reset stops failing allocations once "
                    "that many failures have been injected (a low probability or high interval "
                    "makes that slow) or after a reboot. Pick the smallest count that "
                    "reproduces the failure."
                ).format(times_max=ARMING_BOUNDS["times"][1])
            ),
        ],
        interval: Annotated[
            int | None,
            Field(
                description=(
                    "Optional, 1 to {interval_max}: inject at most once per this many eligible "
                    "allocations (1 is every one). Omitted keeps the guest's current value, "
                    "shown in the returned readback. Out of range: parameter_out_of_range."
                ).format(interval_max=ARMING_BOUNDS["interval"][1])
            ),
        ] = None,
        space: Annotated[
            int | None,
            Field(
                description=(
                    "Optional, 0 to {space_max} bytes: allocation volume that must pass "
                    "before injection can begin, counted per guest. Omitted keeps the guest's "
                    "current value. Out of range: parameter_out_of_range."
                ).format(space_max=ARMING_BOUNDS["space"][1])
            ),
        ] = None,
        verbose: Annotated[
            int | None,
            Field(
                description=(
                    "Optional, 0 to 2: how much each injected failure logs to the guest "
                    "kernel log (0 none, 2 most). Omitted keeps the guest's current value. "
                    "Out of range: parameter_out_of_range."
                )
            ),
        ] = None,
        min_order: Annotated[
            int | None,
            Field(
                description=(
                    "Optional, 0 to 10, fail_page_alloc only: smallest page order (2 to the "
                    "power of the order pages) that can fail. Omitted keeps the guest's "
                    "current value. Sent for failslab it is rejected with reason "
                    "parameter_not_applicable. Out of range: parameter_out_of_range."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Arm one fault-injection knob with bounded parameters on a ready local-libvirt System.

        Requires contributor.

        Arms failslab or fail_page_alloc through the guest's kdive-faultinject helper over the
        System's loopback SSH. Every value is checked before the guest is contacted; the helper
        validates again and writes probability last. An armed knob can fail allocations in every
        task in the guest, including sshd, so arming is bounded: probability is 1 to 100 percent
        and times is a finite count of failures, 1 to {times_max} (-1 and 0 are refused). The
        kernel stops injecting after that many failures, with no clock involved, so a guest that
        can no longer be reached by ssh stops failing allocations once that many failures have
        been injected (a low probability or a high interval makes that slow), or on reboot
        (debugfs state does not survive one). armed false with probability above 0 in the
        readback means the count is already used up. Unset optional parameters keep their
        current guest values, so read them in the returned data before relying on them.

        Returns data.knobs.<knob> with present, armed and the integers probability, interval,
        times, space, verbose (and min-order for fail_page_alloc) read back from the guest after
        the write: the state actually in effect. The helper reply is capped at {byte_cap} bytes
        and a call takes at most about {seconds} seconds. If the call fails with transport_failure
        after the command started (a timeout), the write may or may not have happened: call
        faultinject.inspect, and faultinject.reset if a knob is armed. Concurrent calls on one
        System are not serialized; the last write wins.

        Errors: configuration_error with data.reason unknown_knob (data.accepted_values lists the
        knobs), parameter_out_of_range (data.parameter, data.minimum, data.maximum),
        parameter_not_applicable, knob_absent (this kernel lacks that knob: boot one built with
        CONFIG_FAULT_INJECTION and CONFIG_FAILSLAB or CONFIG_FAIL_PAGE_ALLOC),
        capability_unsupported (provider is not local-libvirt), ssh_not_provisioned (no SSH
        forward), debugfs_not_mounted (mount debugfs in the guest) or helper_not_installed
        (rebuild the guest image with kdive-faultinject staged); not_found (unknown System or
        project); readiness_failure (System is not ready); transport_failure (ssh to the guest
        failed); infrastructure_failure (the helper failed or replied unexpectedly). The next
        actions are faultinject.inspect and faultinject.reset.
        """
        parameters = {
            name: value
            for name, value in (
                ("probability", probability),
                ("times", times),
                ("interval", interval),
                ("space", space),
                ("verbose", verbose),
                ("min-order", min_order),
            )
            if value is not None
        }
        return await set_fault(
            pool,
            current_context(),
            system_id,
            knob,
            parameters,
            resolver=resolver,
            secret_registry=secret_registry,
        )

    @app.tool(
        name="faultinject.reset",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    @_with_limits
    async def faultinject_reset(
        system_id: Annotated[
            str,
            Field(
                description=(
                    "A ready local-libvirt System whose guest image carries the "
                    "kdive-faultinject helper and whose kernel enables fault injection."
                )
            ),
        ],
        knob: Annotated[
            str | None,
            Field(
                description=(
                    "Optional: failslab or fail_page_alloc to reset only that knob. Omitted "
                    "resets every knob this kernel has. Any other name is rejected "
                    "(configuration_error, reason unknown_knob) before the guest is contacted."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Disarm one fault-injection knob, or all of them, on a ready local-libvirt System.

        Requires contributor.

        Writes the kernel defaults through the guest's kdive-faultinject helper over the
        System's loopback SSH: probability 0 (disarmed), times 1, interval 1, space 0, verbose 2
        and, for fail_page_alloc, min-order 1. These are the kernel's compiled-in defaults, not
        the values the knob held before faultinject.set. Probability is written first, so a
        partial failure never leaves injection armed. With knob omitted, a knob the kernel lacks
        is skipped; naming a knob the kernel lacks fails.

        Returns data.knobs for each knob touched, with present, armed (false after a successful
        reset) and the integers read back from the guest. The helper reply is capped at
        {byte_cap} bytes and a call takes at most about {seconds} seconds. If a knob is armed the
        guest may fail this call too (transport_failure): a fault set with faultinject.set stops
        by itself after its times count of failures, or on reboot, so retry after that. If the
        call fails with transport_failure after the command started, call faultinject.inspect
        to see what is in effect. Concurrent calls on one System are not serialized.

        Errors: configuration_error with data.reason unknown_knob, knob_absent (the named knob
        is missing from this kernel), fault_injection_unsupported (knob omitted and neither
        knob exists: boot a kernel with CONFIG_FAULT_INJECTION, CONFIG_FAILSLAB and
        CONFIG_FAIL_PAGE_ALLOC), capability_unsupported (provider is not local-libvirt),
        ssh_not_provisioned (no SSH forward), debugfs_not_mounted (mount debugfs in the guest)
        or helper_not_installed (rebuild the guest image with kdive-faultinject staged);
        not_found (unknown System or project); readiness_failure (System is not ready);
        transport_failure (ssh to the guest failed); infrastructure_failure (the helper failed
        or replied unexpectedly). The next action is faultinject.inspect.
        """
        return await reset_faults(
            pool,
            current_context(),
            system_id,
            knob,
            resolver=resolver,
            secret_registry=secret_registry,
        )
