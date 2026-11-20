"""Device hot-plug ``systems.*`` MCP handlers (ADR-0823, #3700).

Four handlers back ``systems.plug_device``, ``systems.unplug_device``,
``systems.force_unplug_device`` (ADR-0828) and ``systems.list_devices``. Each is invoked through
``with_runtime_for_system`` (which resolves the provider runtime and enforces the required role),
then refuses a provider that lacks the kind with ``capability_unsupported``. Plug and un-plug are
worker jobs because the provider call can wait on the guest, so the synchronous admission here
only validates, writes the ``system_devices`` ledger row and enqueues. The forced un-plug adds the
destructive-operation gate before any state check. ``list_devices`` is a database read.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import SYSTEM_DEVICES, SYSTEMS, devices_for_system
from kdive.domain.capacity.state import SystemDeviceState, SystemState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import System, SystemDevice
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.devices import PLUG_WINDOW_S, UNPLUG_WINDOW_S
from kdive.jobs.payloads import PlugDevicePayload, UnplugDevicePayload
from kdive.log import bind_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import capability_unsupported as _capability_unsupported
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import external_boot_denial as _external_boot_denial
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools.lifecycle._recovery import iso
from kdive.mcp.tools.lifecycle.support._destructive import authorize_destructive
from kdive.mcp.tools.lifecycle.support._device_ops import device_job_active, device_op_in_flight
from kdive.mcp.tools.lifecycle.support._idempotency import keyed_mutation, object_scoped_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.ports.lifecycle import HotplugDeviceKind
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.services.external_boot import (
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)

DEVICE_LIMIT = 8
DISK_MIN_GB = 1
DISK_MAX_GB = 32
NETWORK_MAX_LEN = 64
_NETWORK_RE = re.compile(rf"[A-Za-z0-9._-]{{1,{NETWORK_MAX_LEN}}}")
# Rows that may be un-plugged; `plugging`/`unplugging` are a job in flight.
_UNPLUGGABLE = frozenset(
    {SystemDeviceState.ATTACHED, SystemDeviceState.FAILED, SystemDeviceState.LOST}
)
_TRANSIENT = frozenset({SystemDeviceState.PLUGGING, SystemDeviceState.UNPLUGGING})
_PLUG_KIND = "systems.plug_device"
_UNPLUG_KIND = "systems.unplug_device"
_FORCE_UNPLUG_KIND = "systems.force_unplug_device"
_NEXT_ACTIONS = ["jobs.wait", "systems.list_devices"]


def _conflict(system_id: str, reason: str, detail: str, **data: JsonValue) -> ToolResponse:
    return ToolResponse.failure(
        system_id,
        ErrorCategory.CONFLICT,
        detail=detail,
        suggested_next_actions=["systems.list_devices", "systems.get"],
        data={"reason": reason, **data},
    )


def _capability_refusal(
    runtime: ProviderRuntime, system_id: str, kind: HotplugDeviceKind | None
) -> ToolResponse | None:
    """Refuse a provider with no hot-plug kinds, or one that lacks the requested ``kind``."""
    supported = runtime.support.hotplug_devices
    if supported and (kind is None or kind in supported):
        return None
    return _capability_unsupported(
        system_id,
        capability="hotplug" if kind is None else f"hotplug:{kind.value}",
        provider=runtime.support.component_sources.provider,
        supported=[k.value for k in supported],
    )


def _invalid(system_id: str, reason: str, detail: str) -> ToolResponse:
    return _config_error(system_id, detail=detail, data={"reason": reason})


def _parse_kind(system_id: str, kind: str) -> HotplugDeviceKind | ToolResponse:
    try:
        return HotplugDeviceKind(kind)
    except ValueError:
        accepted = ", ".join(k.value for k in HotplugDeviceKind)
        return _invalid(system_id, "invalid_kind", f"kind must be one of: {accepted}")


def _validate_params(
    system_id: str, kind: HotplugDeviceKind, network: str | None, size_gb: int | None
) -> ToolResponse | None:
    """Validate the kind-specific parameters; ``None`` when they are admissible."""
    if kind is HotplugDeviceKind.NIC:
        if size_gb is not None:
            return _invalid(system_id, "parameter_mismatch", "size_gb applies to kind=disk only")
        if network is not None and not _NETWORK_RE.fullmatch(network):
            return _invalid(
                system_id,
                "invalid_network",
                f"network must be 1..{NETWORK_MAX_LEN} characters of [A-Za-z0-9._-]",
            )
        return None
    if network is not None:
        return _invalid(system_id, "parameter_mismatch", "network applies to kind=nic only")
    if size_gb is None or isinstance(size_gb, bool) or not DISK_MIN_GB <= size_gb <= DISK_MAX_GB:
        return _invalid(
            system_id,
            "invalid_size",
            f"kind=disk requires size_gb, an integer {DISK_MIN_GB}..{DISK_MAX_GB} (GB)",
        )
    return None


async def _ready_system(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, system_id: str
) -> System | ToolResponse:
    """The visible, ``READY`` System under the caller's System lock, or the refusal."""
    system = await SYSTEMS.get(conn, uid)
    if system is None or system.project not in ctx.projects:
        return _config_error(system_id)
    if system.state is not SystemState.READY:
        return _conflict(
            system_id,
            "system_not_ready",
            "devices can be plugged and un-plugged only on a READY System",
            current_status=system.state.value,
        )
    return system


async def _external_boot_refusal(
    conn: AsyncConnection, ctx: RequestContext, system: System
) -> ToolResponse | None:
    try:
        await check_external_boot_admission(
            conn, system.id, ExternalBootOperation.SYSTEM_DEVICE_HOTPLUG, project=system.project
        )
    except ExternalBootDenied as exc:
        return _external_boot_denial(str(system.id), exc, ctx)
    return None


async def _audit_op(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    *,
    tool: str,
    transition: str,
    args: dict[str, JsonValue],
) -> None:
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=tool,
            object_kind="systems",
            object_id=system.id,
            transition=transition,
            args=args,
            project=system.project,
        ),
    )


def _job_envelope(
    device_id: UUID,
    job_id: UUID,
    kind: str,
    state: SystemDeviceState,
    window_s: float,
    *,
    forced: bool = False,
) -> ToolResponse:
    data: dict[str, JsonValue] = {
        "job_id": str(job_id),
        "device_id": str(device_id),
        "kind": kind,
        "state": state.value,
        "settle_timeout_s": int(window_s),
    }
    if forced:
        data["forced"] = True
    return ToolResponse.success(
        str(device_id), "running", suggested_next_actions=list(_NEXT_ACTIONS), data=data
    )


async def _plug_refusal(
    conn: AsyncConnection, ctx: RequestContext, system: System, system_id: str
) -> ToolResponse | None:
    """The admission refusals for a plug, in the order the spec lists them."""
    if (denial := await _external_boot_refusal(conn, ctx, system)) is not None:
        return denial
    rows = await devices_for_system(conn, system.id)
    if await device_job_active(conn, system.id) or any(r.state in _TRANSIENT for r in rows):
        return device_op_in_flight(system_id)
    if len(rows) >= DEVICE_LIMIT:
        return _conflict(
            system_id,
            "device_limit",
            f"a System holds at most {DEVICE_LIMIT} devices; un-plug one first",
            limit=DEVICE_LIMIT,
        )
    return None


async def _admit_plug(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    system_id: str,
    kind: HotplugDeviceKind,
    network: str | None,
    size_gb: int | None,
) -> ToolResponse:
    system = await _ready_system(conn, ctx, uid, system_id)
    if isinstance(system, ToolResponse):
        return system
    refusal = await _plug_refusal(conn, ctx, system, system_id)
    if refusal is not None:
        return refusal
    device_id = uuid4()
    now = datetime.now(UTC)
    await SYSTEM_DEVICES.insert(
        conn,
        SystemDevice(
            id=device_id,
            created_at=now,
            updated_at=now,
            system_id=uid,
            kind=kind.value,
            state=SystemDeviceState.PLUGGING,
            network=network,
            size_gb=size_gb,
        ),
    )
    await _audit_op(
        conn,
        ctx,
        system,
        tool=_PLUG_KIND,
        transition="plug_device",
        args={
            "system_id": system_id,
            "device_id": str(device_id),
            "kind": kind.value,
            "network": network,
            "size_gb": size_gb,
        },
    )
    job = await queue.enqueue(
        conn,
        JobKind.PLUG_DEVICE,
        PlugDevicePayload(system_id=str(uid), device_id=str(device_id)),
        job_authorizing(ctx, system.project),
        f"plug:{device_id}",
        max_attempts=1,
    )
    return _job_envelope(device_id, job.id, kind.value, SystemDeviceState.PLUGGING, PLUG_WINDOW_S)


async def _keyed_admission(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    uid: UUID,
    system_id: str,
    *,
    kind: str,
    arguments: Mapping[str, object] | None = None,
    idempotency_key: str | None,
    admit: Callable[[AsyncConnection], Awaitable[ToolResponse]],
    gate: Callable[[AsyncConnection, System], Awaitable[ToolResponse | None]] | None = None,
) -> ToolResponse:
    """Run ``admit`` under the System lock and the optional replay key.

    ``gate`` runs after the in-project check and before the lock and the replay lookup, so a
    caller who no longer passes it is denied rather than replayed.
    """
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            if gate is not None and (denial := await gate(conn, system)) is not None:
                return denial
            # SAVEPOINT-style nesting: the lock is held to the request's own commit, so the row
            # insert and the enqueue below are one transaction under it.
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, uid):
                return await keyed_mutation(
                    conn,
                    idempotency_key=idempotency_key,
                    principal=ctx.principal,
                    project=system.project,
                    kind=object_scoped_kind(kind, uid, arguments),
                    do_work=lambda: admit(conn),
                )


async def plug_device(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    runtime: ProviderRuntime,
    *,
    system_id: str,
    kind: str,
    network: str | None,
    size_gb: int | None,
    idempotency_key: str | None,
) -> ToolResponse:
    """Admit a hot-plug on a ``READY`` System: ledger row ``plugging`` plus a `plug_device` job."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    parsed = _parse_kind(system_id, kind)
    if isinstance(parsed, ToolResponse):
        # A provider with no hot-plug kinds refuses before an unparseable kind is judged.
        return _capability_refusal(runtime, system_id, None) or parsed
    refusal = _capability_refusal(runtime, system_id, parsed) or _validate_params(
        system_id, parsed, network, size_gb
    )
    if refusal is not None:
        return refusal
    return await _keyed_admission(
        pool,
        ctx,
        uid,
        system_id,
        kind=_PLUG_KIND,
        arguments={"kind": parsed.value, "network": network, "size_gb": size_gb},
        idempotency_key=idempotency_key,
        admit=lambda conn: _admit_plug(conn, ctx, uid, system_id, parsed, network, size_gb),
    )


async def _unplug_target(
    conn: AsyncConnection, system: System, system_id: str, device_id: UUID
) -> SystemDevice | ToolResponse:
    row = await SYSTEM_DEVICES.get(conn, device_id)
    if row is None or row.system_id != system.id:
        return _not_found(str(device_id))
    if (
        row.state not in _UNPLUGGABLE
        or await device_job_active(conn, system.id)
        or any(r.state in _TRANSIENT for r in await devices_for_system(conn, system.id))
    ):
        return device_op_in_flight(system_id)
    return row


async def _admit_unplug(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    system_id: str,
    device_id: UUID,
    attempt: str,
) -> ToolResponse:
    system = await _ready_system(conn, ctx, uid, system_id)
    if isinstance(system, ToolResponse):
        return system
    if (denial := await _external_boot_refusal(conn, ctx, system)) is not None:
        return denial
    row = await _unplug_target(conn, system, system_id, device_id)
    if isinstance(row, ToolResponse):
        return row
    await SYSTEM_DEVICES.update_state(conn, row.id, SystemDeviceState.UNPLUGGING)
    await _audit_op(
        conn,
        ctx,
        system,
        tool=_UNPLUG_KIND,
        transition="unplug_device",
        args={"system_id": system_id, "device_id": str(device_id), "from_state": row.state.value},
    )
    job = await queue.enqueue(
        conn,
        JobKind.UNPLUG_DEVICE,
        UnplugDevicePayload(system_id=str(uid), device_id=str(device_id), from_state=row.state),
        job_authorizing(ctx, system.project),
        f"unplug:{device_id}:{attempt}",
        max_attempts=1,
    )
    return _job_envelope(device_id, job.id, row.kind, SystemDeviceState.UNPLUGGING, UNPLUG_WINDOW_S)


async def unplug_device(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    runtime: ProviderRuntime,
    *,
    system_id: str,
    device_id: str,
    idempotency_key: str | None,
) -> ToolResponse:
    """Admit a graceful un-plug of an ``attached``, ``failed`` or ``lost`` ledger row."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    device_uid = _as_uuid(device_id)
    if device_uid is None:
        return _invalid_uuid_error("device_id", device_id)
    refusal = _capability_refusal(runtime, system_id, None)
    if refusal is not None:
        return refusal
    # A fresh attempt token lets a retry after a terminal job enqueue new work (dedup is per key).
    attempt = idempotency_key if idempotency_key is not None else uuid4().hex
    return await _keyed_admission(
        pool,
        ctx,
        uid,
        system_id,
        # The device is the target; the System id is folded in by `_keyed_admission`.
        kind=object_scoped_kind(_UNPLUG_KIND, device_uid),
        idempotency_key=idempotency_key,
        admit=lambda conn: _admit_unplug(conn, ctx, uid, system_id, device_uid, attempt),
    )


def _forced_refusal(
    runtime: ProviderRuntime, system_id: str, kind: HotplugDeviceKind | None
) -> ToolResponse | None:
    """Refuse a provider with no forced kind, or one that cannot force ``kind`` (ADR-0828)."""
    forced = runtime.support.forced_unplug_devices
    if forced and (kind is None or kind in forced):
        return None
    return _capability_unsupported(
        system_id,
        capability="forced_unplug" if kind is None else f"forced_unplug:{kind.value}",
        provider=runtime.support.component_sources.provider,
        supported=[k.value for k in forced],
    )


async def _force_unplug_target(
    conn: AsyncConnection, runtime: ProviderRuntime, system: System, device_id: UUID
) -> SystemDevice | ToolResponse:
    """The row a forced un-plug may act on, or the first refusal, in the spec's order."""
    system_id = str(system.id)
    row = await SYSTEM_DEVICES.get(conn, device_id)
    if row is None or row.system_id != system.id:
        return _not_found(str(device_id))
    if await device_job_active(conn, system.id) or any(
        r.state in _TRANSIENT for r in await devices_for_system(conn, system.id)
    ):
        return device_op_in_flight(system_id)
    if row.state is SystemDeviceState.LOST:
        return ToolResponse.failure(
            system_id,
            ErrorCategory.CONFLICT,
            detail="a lost row has no live device to force; un-plug it gracefully to clear it",
            suggested_next_actions=["systems.unplug_device"],
            data={"reason": "device_not_forceable"},
        )
    return _forced_refusal(runtime, system_id, HotplugDeviceKind(row.kind)) or row


async def _admit_force_unplug(
    conn: AsyncConnection,
    ctx: RequestContext,
    runtime: ProviderRuntime,
    uid: UUID,
    system_id: str,
    device_id: UUID,
    attempt: str,
) -> ToolResponse:
    system = await _ready_system(conn, ctx, uid, system_id)
    if isinstance(system, ToolResponse):
        return system
    if (denial := await _external_boot_refusal(conn, ctx, system)) is not None:
        return denial
    row = await _force_unplug_target(conn, runtime, system, device_id)
    if isinstance(row, ToolResponse):
        return row
    await SYSTEM_DEVICES.update_state(conn, row.id, SystemDeviceState.UNPLUGGING)
    await _audit_op(
        conn,
        ctx,
        system,
        tool=_FORCE_UNPLUG_KIND,
        transition="force_unplug_device",
        args={
            "system_id": system_id,
            "device_id": str(device_id),
            "kind": row.kind,
            "from_state": row.state.value,
        },
    )
    job = await queue.enqueue(
        conn,
        JobKind.FORCE_UNPLUG_DEVICE,
        UnplugDevicePayload(system_id=str(uid), device_id=str(device_id), from_state=row.state),
        job_authorizing(ctx, system.project),
        f"force_unplug:{device_id}:{attempt}",
        max_attempts=1,
    )
    return _job_envelope(
        device_id, job.id, row.kind, SystemDeviceState.UNPLUGGING, UNPLUG_WINDOW_S, forced=True
    )


async def force_unplug_device(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    runtime: ProviderRuntime,
    resolver: ProviderResolver,
    *,
    system_id: str,
    device_id: str,
    idempotency_key: str | None,
) -> ToolResponse:
    """Gate and admit a forced un-plug of an ``attached`` or ``failed`` row (ADR-0828).

    Order: UUID parse, provider capability, the destructive-operation gate (before the replay
    lookup and every device or state check), then the admission under the System lock.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    device_uid = _as_uuid(device_id)
    if device_uid is None:
        return _invalid_uuid_error("device_id", device_id)
    refusal = _forced_refusal(runtime, system_id, None)
    if refusal is not None:
        return refusal
    # Always a fresh token: replay is `keyed_mutation`'s, and a key-derived dedup key would let a
    # replay miss re-adopt a terminal job and report it as the new one.
    attempt = uuid4().hex

    async def gate(conn: AsyncConnection, system: System) -> ToolResponse | None:
        return await authorize_destructive(
            conn,
            ctx,
            system,
            uid,
            JobKind.FORCE_UNPLUG_DEVICE,
            resolver=resolver,
            tool=_FORCE_UNPLUG_KIND,
            audit_args={"device_id": str(device_uid)},
        )

    return await _keyed_admission(
        pool,
        ctx,
        uid,
        system_id,
        kind=object_scoped_kind(_FORCE_UNPLUG_KIND, device_uid),
        idempotency_key=idempotency_key,
        admit=lambda conn: _admit_force_unplug(
            conn, ctx, runtime, uid, system_id, device_uid, attempt
        ),
        gate=gate,
    )


def _device_item(row: SystemDevice) -> dict[str, JsonValue]:
    deadline = row.settle_deadline if row.state in _TRANSIENT else None
    return {
        "device_id": str(row.id),
        "kind": row.kind,
        "state": row.state.value,
        "network": row.network,
        "size_gb": row.size_gb,
        "identity": dict(row.identity),
        "detail": row.detail,
        "settle_deadline": iso(deadline),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


async def list_devices(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    runtime: ProviderRuntime,
    *,
    system_id: str,
) -> ToolResponse:
    """Return a System's device ledger oldest-first from Postgres (no provider round-trip)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    refusal = _capability_refusal(runtime, system_id, None)
    if refusal is not None:
        return refusal
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            rows = await devices_for_system(conn, uid)
            # The database clock stamped `settle_deadline`, so it is the reference clock for it.
            async with conn.cursor() as cur:
                await cur.execute("SELECT now()")
                server_time = (await cur.fetchone() or (datetime.now(UTC),))[0]
    return ToolResponse.success(
        system_id,
        "ok",
        suggested_next_actions=["systems.plug_device", "systems.unplug_device"],
        data={
            "devices": [_device_item(row) for row in rows],
            "device_count": len(rows),
            "device_limit": DEVICE_LIMIT,
            "server_time": iso(server_time),
        },
    )


__all__ = [
    "DEVICE_LIMIT",
    "DISK_MAX_GB",
    "DISK_MIN_GB",
    "NETWORK_MAX_LEN",
    "force_unplug_device",
    "list_devices",
    "plug_device",
    "unplug_device",
]
