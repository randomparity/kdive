"""Worker handlers for ``plug_device``, ``unplug_device``, ``force_unplug_device`` (ADR-0823/0828).

Each current job claim drives one ``system_devices`` row and its provider device. It records
the outcome before returning; stale claims leave the ledger alone. A worker that dies mid-call
leaves a row the reconciler repairs without claiming to know the provider's physical state.

The observation window starts with the provider step on the worker's monotonic clock. Started
mutations drain under the System lock, including on cancellation; they may finish after the window.
Pending outcomes time out before further observation, retaining available recovery evidence.
Only the credential-bound current job claim may write the ledger (ADR-0831).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.locks import (
    LockScope,
    require_top_level_transaction,
    scoped_session_advisory_lock,
)
from kdive.db.repositories import (
    SYSTEM_DEVICES,
    SYSTEMS,
    ObjectNotFound,
    record_device_plug,
    settle_device,
    start_device_settle_window,
)
from kdive.domain.capacity.state import IllegalTransition, SystemDeviceState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System, SystemDevice
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import PlugDevicePayload, UnplugDevicePayload, load_payload
from kdive.jobs.provider_context import set_provider_kind, take_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import SystemHandleResolver
from kdive.providers.ports.lifecycle import DeviceHotplugger, DeviceSpec, HotplugDeviceKind
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)

PLUG_WINDOW_S: Final = 60.0
UNPLUG_WINDOW_S: Final = 30.0
POLL_INTERVAL_S: Final = 1.0
_DETAIL_MAX_CHARS: Final = 500
_REFUSAL_REASONS: Final = frozenset({"unplug_refused", "handle_unknown"})
_SETTLE_TIMEOUT: Final = "settle_timeout"
_UNEXPECTED_DETAIL: Final = "unexpected worker error; the device may or may not be in the guest"


class _StaleDeviceAttempt(Exception):
    """The claimed job changed before a device ledger write."""


@dataclass(frozen=True)
class _DeviceAttempt:
    conn: AsyncConnection
    job: Job
    incarnation_credential: SecretStr

    @asynccontextmanager
    async def write(self) -> AsyncGenerator[None]:
        async with self.conn.transaction(), self.conn.cursor() as cur:
            await cur.execute(
                "SELECT public.lock_device_job_attempt(%s, "
                "sha256(convert_to(%s, 'UTF8')), %s, %s, %s, %s, %s)",
                (
                    self.job.id,
                    self.incarnation_credential.get_secret_value(),
                    self.job.attempt,
                    self.job.worker_id,
                    self.job.created_at,
                    self.job.kind.value,
                    Jsonb(self.job.payload),
                ),
            )
            row = await cur.fetchone()
            if row is None or not row[0]:
                raise _StaleDeviceAttempt
            yield


async def _run_owned_device(
    conn: AsyncConnection,
    job: Job,
    incarnation_credential: SecretStr,
    system_id: UUID,
    device_id: UUID,
    operation: Callable[[_DeviceAttempt], Awaitable[str | None]],
) -> str | None:
    provider_kind: str | None = None

    async def protected() -> str | None:
        nonlocal provider_kind
        try:
            require_top_level_transaction(conn, "device operation")
            previous_autocommit = conn.autocommit
            if not previous_autocommit:
                await conn.set_autocommit(True)
            try:
                async with scoped_session_advisory_lock(conn, LockScope.SYSTEM, system_id):
                    attempt = _DeviceAttempt(conn, job, incarnation_credential)
                    try:
                        async with attempt.write():
                            pass
                        return await operation(attempt)
                    except _StaleDeviceAttempt:
                        _log.info(
                            "device job %s lost its claim; leaving the ledger unchanged", job.id
                        )
                        return str(device_id)
            finally:
                if not previous_autocommit:
                    await conn.set_autocommit(False)
        finally:
            provider_kind = take_provider_kind()

    task = asyncio.create_task(protected())
    # Python 3.14 reports failures of cancelled shield waiters; drain a non-raising future.
    drain = asyncio.gather(task, return_exceptions=True)
    try:
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    continue
            raise
        return task.result()
    finally:
        if provider_kind is not None:
            set_provider_kind(provider_kind)


class _Window:
    """One job's settle window on the monotonic clock."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._started = time.monotonic()

    def remaining(self) -> float:
        return self._seconds - self.waited()

    def waited(self) -> float:
        return time.monotonic() - self._started


def _settle_timeout(window: _Window) -> CategorizedError:
    waited = round(window.waited(), 1)
    return CategorizedError(
        f"the provider did not settle the device within {waited} s",
        category=ErrorCategory.CONTROL_FAILURE,
        details={"reason": _SETTLE_TIMEOUT, "waited_s": waited},
        terminal=True,
    )


async def _call_within[T](window: _Window, call: Callable[[], T]) -> T:
    """Run a blocking provider call in a thread, bounded by what is left of the window."""
    remaining = window.remaining()
    if remaining <= 0:
        raise _settle_timeout(window)
    try:
        return await asyncio.wait_for(asyncio.to_thread(call), timeout=remaining)
    except TimeoutError:
        if window.remaining() > 0:  # the provider's own timeout, not our window
            raise
        raise _settle_timeout(window) from None


async def _call_mutation[T](window: _Window, call: Callable[[], T]) -> T:
    """Drain a started mutation; its enclosing attempt retains the System lock."""
    if window.remaining() <= 0:
        raise _settle_timeout(window)
    try:
        return await asyncio.to_thread(call)
    except TimeoutError:
        if window.remaining() > 0:
            raise
        raise _settle_timeout(window) from None


async def _poll_until(
    window: _Window,
    is_attached: Callable[[], bool],
    *,
    attached: bool,
    interval_s: float,
    mutating: bool = False,
) -> None:
    """Bound observations, draining callbacks that also remove backing storage."""
    invoke = _call_mutation if mutating else _call_within
    while await invoke(window, is_attached) is not attached:
        await asyncio.sleep(max(0.0, min(interval_s, window.remaining())))


def _detail(redactor: Redactor, error: CategorizedError) -> str:
    return redactor.redact_text(str(error))[:_DETAIL_MAX_CHARS]


async def _settle(
    attempt: _DeviceAttempt,
    device_id: UUID,
    new_state: SystemDeviceState,
    *,
    detail: str | None = None,
) -> None:
    """Settle the row, tolerating one that is gone or already moved on (a racing teardown)."""
    try:
        async with attempt.write():
            await settle_device(attempt.conn, device_id, new_state, detail=detail)
    except IllegalTransition, ObjectNotFound:
        _log.info("device row %s gone or already settled; not moving to %s", device_id, new_state)


async def _delete_row(attempt: _DeviceAttempt, device_id: UUID) -> None:
    async with attempt.write():
        await SYSTEM_DEVICES.delete(attempt.conn, device_id)


async def _device_row(
    conn: AsyncConnection, system_id: UUID, device_id: UUID
) -> SystemDevice | None:
    row = await SYSTEM_DEVICES.get(conn, device_id)
    return row if row is not None and row.system_id == system_id else None


async def _device_system(conn: AsyncConnection, system_id: UUID) -> System:
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "device target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
            terminal=True,
        )
    return system


async def _hotplug_ports(
    conn: AsyncConnection, resolver: ProviderResolver, system_id: UUID
) -> tuple[DeviceHotplugger, SystemHandleResolver]:
    binding = await resolver.binding_for_system(conn, system_id)
    set_provider_kind(binding.kind.value)
    if (
        binding.runtime.device_hotplug is None
    ):  # Defense in depth: the tool gates on the capability.
        raise CategorizedError(
            "provider does not support device hot-plug",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system_id)},
        )
    return binding.runtime.device_hotplug, binding.runtime.system_handle


def _not_ready(system_id: UUID, state: SystemState) -> CategorizedError:
    return CategorizedError(
        "device hot-plug requires a READY System",
        category=ErrorCategory.CONFLICT,
        details={
            "reason": "system_not_ready",
            "system_id": str(system_id),
            "current_status": state.value,
        },
        terminal=True,
    )


async def plug_device_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    incarnation_credential: SecretStr,
    window_s: float = PLUG_WINDOW_S,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> str | None:
    """Plug and settle only the current claimed attempt, draining its provider mutations."""
    payload = load_payload(job, PlugDevicePayload)
    system_id, device_id = UUID(payload.system_id), UUID(payload.device_id)
    return await _run_owned_device(
        conn,
        job,
        incarnation_credential,
        system_id,
        device_id,
        lambda attempt: _plug_device(
            attempt,
            system_id,
            device_id,
            resolver=resolver,
            secret_registry=secret_registry,
            window_s=window_s,
            poll_interval_s=poll_interval_s,
        ),
    )


async def _plug_device(
    attempt: _DeviceAttempt,
    system_id: UUID,
    device_id: UUID,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    window_s: float,
    poll_interval_s: float,
) -> str | None:
    conn = attempt.conn
    row = await _device_row(conn, system_id, device_id)
    if row is None or row.state is not SystemDeviceState.PLUGGING:
        return str(device_id)
    redactor = Redactor(registry=secret_registry)
    system = await _device_system(conn, system_id)
    try:
        if system.state is not SystemState.READY:  # Protect raced teardown or restore.
            raise _not_ready(system_id, system.state)
        hotplugger, system_handle = await _hotplug_ports(conn, resolver, system_id)
        domain = system_handle(system.id, system.domain_name)
        spec = DeviceSpec(HotplugDeviceKind(row.kind), str(device_id), row.network, row.size_gb)
        await _drive_plug(attempt, hotplugger, domain, spec, window_s, poll_interval_s)
    except CategorizedError as exc:
        if exc.category is ErrorCategory.CONFIGURATION_ERROR and await _nothing_attached(
            conn, system_id, device_id
        ):
            await _delete_row(attempt, device_id)
        else:
            await _settle(
                attempt, device_id, SystemDeviceState.FAILED, detail=_detail(redactor, exc)
            )
        exc.terminal = True
        raise
    except Exception:
        await _settle(attempt, device_id, SystemDeviceState.FAILED, detail=_UNEXPECTED_DETAIL)
        raise
    return str(device_id)


async def _nothing_attached(conn: AsyncConnection, system_id: UUID, device_id: UUID) -> bool:
    """True while the provider has returned no handle, so a rejected plug attached nothing."""
    row = await _device_row(conn, system_id, device_id)
    return row is not None and row.provider_handle is None


async def _drive_plug(
    attempt: _DeviceAttempt,
    hotplugger: DeviceHotplugger,
    domain: str,
    spec: DeviceSpec,
    window_s: float,
    poll_interval_s: float,
) -> None:
    conn = attempt.conn
    device_id = UUID(spec.device_id)
    window = _Window(window_s)
    async with attempt.write():
        await start_device_settle_window(conn, device_id, window_s)
    outcome = await _call_mutation(window, functools.partial(hotplugger.plug, domain, spec))
    async with attempt.write():
        await record_device_plug(conn, device_id, outcome.provider_handle, outcome.identity)
    if not outcome.settled:
        await _poll_until(
            window,
            functools.partial(
                hotplugger.is_attached,
                domain,
                spec.device_id,
                outcome.provider_handle,
                kind=spec.kind,
            ),
            attached=True,
            interval_s=poll_interval_s,
            mutating=spec.kind is HotplugDeviceKind.DISK,
        )
    await _settle(attempt, device_id, SystemDeviceState.ATTACHED)


async def unplug_device_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    incarnation_credential: SecretStr,
    window_s: float = UNPLUG_WINDOW_S,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> str | None:
    """Unplug only the current claimed attempt, retaining ownership through mutation drain."""
    payload = load_payload(job, UnplugDevicePayload)
    system_id, device_id = UUID(payload.system_id), UUID(payload.device_id)
    return await _run_owned_device(
        conn,
        job,
        incarnation_credential,
        system_id,
        device_id,
        lambda attempt: _unplug_device(
            attempt,
            system_id,
            device_id,
            payload.from_state,
            resolver=resolver,
            secret_registry=secret_registry,
            window_s=window_s,
            poll_interval_s=poll_interval_s,
            forced=job.kind is JobKind.FORCE_UNPLUG_DEVICE,
        ),
    )


async def _unplug_device(
    attempt: _DeviceAttempt,
    system_id: UUID,
    device_id: UUID,
    from_state: SystemDeviceState,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    window_s: float,
    poll_interval_s: float,
    forced: bool,
) -> str | None:
    conn = attempt.conn
    row = await _device_row(conn, system_id, device_id)
    if row is None or row.state is SystemDeviceState.PLUGGING:
        return str(device_id)
    redactor = Redactor(registry=secret_registry)
    system = await _device_system(conn, system_id)
    try:
        if system.state is not SystemState.READY:  # Protect raced teardown or restore.
            raise _not_ready(system_id, system.state)
        if row.state is not SystemDeviceState.UNPLUGGING:
            async with attempt.write():
                await settle_device(conn, device_id, SystemDeviceState.UNPLUGGING)
        hotplugger, system_handle = await _hotplug_ports(conn, resolver, system_id)
        domain = system_handle(system.id, system.domain_name)
        await _drive_unplug(
            attempt, hotplugger, domain, row, window_s, poll_interval_s, forced=forced
        )
    except CategorizedError as exc:
        await _settle_failed_unplug(attempt, device_id, from_state, exc, redactor)
        exc.terminal = True
        raise
    except Exception:
        await _settle(attempt, device_id, SystemDeviceState.FAILED, detail=_UNEXPECTED_DETAIL)
        raise
    return str(device_id)


async def _settle_failed_unplug(
    attempt: _DeviceAttempt,
    device_id: UUID,
    from_state: SystemDeviceState,
    error: CategorizedError,
    redactor: Redactor,
) -> None:
    """Place the row after a failed un-plug: back where it started, or ``failed``."""
    returns = error.details.get("reason") in _REFUSAL_REASONS or (
        error.details.get("reason") == "system_not_ready"
    )
    if not returns:
        await _settle(attempt, device_id, SystemDeviceState.FAILED, detail=_detail(redactor, error))
        return
    detail = _detail(redactor, error) if from_state is SystemDeviceState.FAILED else None
    await _settle(attempt, device_id, from_state, detail=detail)


async def _drive_unplug(
    attempt: _DeviceAttempt,
    hotplugger: DeviceHotplugger,
    domain: str,
    row: SystemDevice,
    window_s: float,
    poll_interval_s: float,
    *,
    forced: bool = False,
) -> None:
    conn = attempt.conn
    window = _Window(window_s)
    async with attempt.write():
        await start_device_settle_window(conn, row.id, window_s)
    device_id = str(row.id)
    kind = HotplugDeviceKind(row.kind)
    if forced:
        call = functools.partial(
            hotplugger.force_unplug, domain, device_id, row.provider_handle, kind=kind
        )
    else:
        call = functools.partial(
            hotplugger.unplug, domain, device_id, row.provider_handle, kind=kind
        )
    removed = await _call_mutation(window, call)
    if not removed:
        await _poll_until(
            window,
            functools.partial(
                hotplugger.is_attached, domain, device_id, row.provider_handle, kind=kind
            ),
            attached=False,
            interval_s=poll_interval_s,
            mutating=kind is HotplugDeviceKind.DISK,
        )
    await _delete_row(attempt, row.id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    incarnation_credential: SecretStr,
) -> None:
    """Bind the plug and un-plug device handlers."""
    registry.register(
        JobKind.PLUG_DEVICE,
        lambda conn, job: plug_device_handler(
            conn,
            job,
            resolver=resolver,
            secret_registry=secret_registry,
            incarnation_credential=incarnation_credential,
        ),
    )
    for kind in (JobKind.UNPLUG_DEVICE, JobKind.FORCE_UNPLUG_DEVICE):
        registry.register(
            kind,
            lambda conn, job: unplug_device_handler(
                conn,
                job,
                resolver=resolver,
                secret_registry=secret_registry,
                incarnation_credential=incarnation_credential,
            ),
        )
