"""Worker handlers for ``plug_device``, ``unplug_device``, ``force_unplug_device`` (ADR-0823/0828).

Each job drives one ``system_devices`` row and the provider's :class:`DeviceHotplugger` to the
states in the spec's transition table. The row is the durable record, so every outcome is written
to it before the job ends and a worker that dies mid-call leaves a row the reconciler repair fails.

The settle window is measured on the worker's monotonic clock from the start of the job, covers the
provider call and every poll, and each provider call carries the window that remains. At the
deadline the row becomes ``failed`` and the job fails ``control_failure`` with
``reason: settle_timeout``; the device may still appear or vanish afterwards.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection

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
from kdive.domain.lifecycle.records import SystemDevice
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import PlugDevicePayload, UnplugDevicePayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.lifecycle import DeviceHotplugger, DeviceSpec, HotplugDeviceKind
from kdive.providers.shared.runtime_paths import domain_name_for
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


async def _poll_until(
    window: _Window, is_attached: Callable[[], bool], *, attached: bool, interval_s: float
) -> None:
    """Poll ``is_attached`` until it reports ``attached``; the window bounds every call."""
    while await _call_within(window, is_attached) is not attached:
        await asyncio.sleep(max(0.0, min(interval_s, window.remaining())))


def _detail(redactor: Redactor, error: CategorizedError) -> str:
    return redactor.redact_text(str(error))[:_DETAIL_MAX_CHARS]


async def _settle(
    conn: AsyncConnection,
    device_id: UUID,
    new_state: SystemDeviceState,
    *,
    detail: str | None = None,
) -> None:
    """Settle the row, tolerating one that is gone or already moved on (a racing teardown)."""
    try:
        await settle_device(conn, device_id, new_state, detail=detail)
    except IllegalTransition, ObjectNotFound:
        _log.info("device row %s gone or already settled; not moving to %s", device_id, new_state)


async def _delete_row(conn: AsyncConnection, device_id: UUID) -> None:
    async with conn.transaction():
        await SYSTEM_DEVICES.delete(conn, device_id)


async def _device_row(
    conn: AsyncConnection, system_id: UUID, device_id: UUID
) -> SystemDevice | None:
    row = await SYSTEM_DEVICES.get(conn, device_id)
    return row if row is not None and row.system_id == system_id else None


async def _system_domain_and_state(
    conn: AsyncConnection, system_id: UUID
) -> tuple[str, SystemState]:
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "device target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
            terminal=True,
        )
    return system.domain_name or domain_name_for(system_id), system.state


async def _hotplugger(
    conn: AsyncConnection, resolver: ProviderResolver, system_id: UUID
) -> DeviceHotplugger:
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
    return binding.runtime.device_hotplug


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
    window_s: float = PLUG_WINDOW_S,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> str | None:
    """Plug the row's device and drive it ``plugging -> attached|failed`` (or delete it).

    A row that is gone (a retry after a delete) or no longer ``plugging`` (an at-least-once
    redelivery after the outcome landed) is a no-op success. A port rejection with
    ``configuration_error`` deletes the row, since nothing was attached.
    """
    payload = load_payload(job, PlugDevicePayload)
    system_id, device_id = UUID(payload.system_id), UUID(payload.device_id)
    row = await _device_row(conn, system_id, device_id)
    if row is None or row.state is not SystemDeviceState.PLUGGING:
        return str(device_id)
    redactor = Redactor(registry=secret_registry)
    domain, state = await _system_domain_and_state(conn, system_id)
    try:
        if state is not SystemState.READY:  # Re-verify at start: a mid-flight teardown or revert.
            raise _not_ready(system_id, state)
        hotplugger = await _hotplugger(conn, resolver, system_id)
        spec = DeviceSpec(HotplugDeviceKind(row.kind), str(device_id), row.network, row.size_gb)
        await _drive_plug(conn, hotplugger, domain, spec, window_s, poll_interval_s)
    except CategorizedError as exc:
        if exc.category is ErrorCategory.CONFIGURATION_ERROR and await _nothing_attached(
            conn, system_id, device_id
        ):
            await _delete_row(conn, device_id)
        else:
            await _settle(conn, device_id, SystemDeviceState.FAILED, detail=_detail(redactor, exc))
        exc.terminal = True
        raise
    except Exception:
        await _settle(conn, device_id, SystemDeviceState.FAILED, detail=_UNEXPECTED_DETAIL)
        raise
    return str(device_id)


async def _nothing_attached(conn: AsyncConnection, system_id: UUID, device_id: UUID) -> bool:
    """True while the provider has returned no handle, so a rejected plug attached nothing."""
    row = await _device_row(conn, system_id, device_id)
    return row is not None and row.provider_handle is None


async def _drive_plug(
    conn: AsyncConnection,
    hotplugger: DeviceHotplugger,
    domain: str,
    spec: DeviceSpec,
    window_s: float,
    poll_interval_s: float,
) -> None:
    device_id = UUID(spec.device_id)
    window = _Window(window_s)
    await start_device_settle_window(conn, device_id, window_s)
    outcome = await _call_within(window, functools.partial(hotplugger.plug, domain, spec))
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
        )
    await _settle(conn, device_id, SystemDeviceState.ATTACHED)


async def unplug_device_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    window_s: float = UNPLUG_WINDOW_S,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> str | None:
    """Un-plug the row's device and delete the row, or return it to the state it started from.

    A row that is gone is a no-op success. A row already ``failed`` (an earlier attempt of this
    job) proceeds. A provider refusal (``unplug_refused``, ``handle_unknown``) returns the row to
    ``from_state``; a settle timeout or any other provider fault makes it ``failed``. In every
    case the device may still be in the guest.
    """
    payload = load_payload(job, UnplugDevicePayload)
    system_id, device_id = UUID(payload.system_id), UUID(payload.device_id)
    row = await _device_row(conn, system_id, device_id)
    if row is None or row.state is SystemDeviceState.PLUGGING:
        return str(device_id)
    redactor = Redactor(registry=secret_registry)
    domain, state = await _system_domain_and_state(conn, system_id)
    try:
        if state is not SystemState.READY:  # Re-verify at start: a mid-flight teardown or revert.
            raise _not_ready(system_id, state)
        if row.state is not SystemDeviceState.UNPLUGGING:
            await settle_device(conn, device_id, SystemDeviceState.UNPLUGGING)
        hotplugger = await _hotplugger(conn, resolver, system_id)
        forced = job.kind is JobKind.FORCE_UNPLUG_DEVICE
        await _drive_unplug(conn, hotplugger, domain, row, window_s, poll_interval_s, forced=forced)
    except CategorizedError as exc:
        await _settle_failed_unplug(conn, device_id, payload.from_state, exc, redactor)
        exc.terminal = True
        raise
    except Exception:
        await _settle(conn, device_id, SystemDeviceState.FAILED, detail=_UNEXPECTED_DETAIL)
        raise
    return str(device_id)


async def _settle_failed_unplug(
    conn: AsyncConnection,
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
        await _settle(conn, device_id, SystemDeviceState.FAILED, detail=_detail(redactor, error))
        return
    detail = _detail(redactor, error) if from_state is SystemDeviceState.FAILED else None
    await _settle(conn, device_id, from_state, detail=detail)


async def _drive_unplug(
    conn: AsyncConnection,
    hotplugger: DeviceHotplugger,
    domain: str,
    row: SystemDevice,
    window_s: float,
    poll_interval_s: float,
    *,
    forced: bool = False,
) -> None:
    window = _Window(window_s)
    await start_device_settle_window(conn, row.id, window_s)
    device_id = str(row.id)
    kind = HotplugDeviceKind(row.kind)
    if forced:
        call = functools.partial(hotplugger.force_unplug, domain, device_id, row.provider_handle)
    else:
        call = functools.partial(
            hotplugger.unplug, domain, device_id, row.provider_handle, kind=kind
        )
    removed = await _call_within(window, call)
    if not removed:
        await _poll_until(
            window,
            functools.partial(
                hotplugger.is_attached, domain, device_id, row.provider_handle, kind=kind
            ),
            attached=False,
            interval_s=poll_interval_s,
        )
    await _delete_row(conn, row.id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Bind the plug and un-plug device handlers."""
    registry.register(
        JobKind.PLUG_DEVICE,
        lambda conn, job: plug_device_handler(
            conn, job, resolver=resolver, secret_registry=secret_registry
        ),
    )
    for kind in (JobKind.UNPLUG_DEVICE, JobKind.FORCE_UNPLUG_DEVICE):
        registry.register(
            kind,
            lambda conn, job: unplug_device_handler(
                conn, job, resolver=resolver, secret_registry=secret_registry
            ),
        )
