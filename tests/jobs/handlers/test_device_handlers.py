"""Worker handlers for plug_device / unplug_device against the fault-inject port (#3699, ADR-0823).

Every handler runs over the kdive_worker login with autocommit, as the worker does, so the grants
and the commit points (handle recorded before the settle poll) are exercised. The provider is the
real fault-inject port; ``hotplug_behaviour`` and the injected-fault plane pick the outcome.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import SYSTEM_DEVICES, SYSTEMS
from kdive.domain.capacity.state import JobState, SystemDeviceState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import SystemDevice
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.devices import plug_device_handler, unplug_device_handler
from kdive.providers.fault_inject.faulting.engine import FaultEngine
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.fault_inject.lifecycle.faulted import FaultedHotplug
from kdive.providers.fault_inject.lifecycle.hotplug import (
    ADVERTISED_KINDS,
    FaultInjectHotplug,
    HotplugBehaviour,
)
from kdive.providers.ports.lifecycle import DeviceHotplugger, DeviceSpec, HotplugDeviceKind
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.conftest import _RoleDsns
from tests.db.system_ledger_support import connect, seed_system
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 11, 19, tzinfo=UTC)
_WINDOW_S = 0.3
_POLL_S = 0.01
_ATTACHED, _FAILED, _LOST = (
    SystemDeviceState.ATTACHED,
    SystemDeviceState.FAILED,
    SystemDeviceState.LOST,
)


class _Untouched:
    """A port that fails the test if the handler reaches the provider."""

    def plug(self, domain_name: str, spec: DeviceSpec) -> Any:
        raise AssertionError("provider plug must not be called")

    def unplug(
        self,
        domain_name: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        raise AssertionError("provider unplug must not be called")

    def is_attached(
        self,
        domain_name: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        raise AssertionError("provider is_attached must not be called")

    def release_all(self, domain_name: str) -> None:
        raise AssertionError("provider release_all must not be called")


class _Failing(_Untouched):
    """A port whose plug raises a provider error carrying ``message``."""

    def __init__(self, message: str) -> None:
        self._message = message

    def plug(self, domain_name: str, spec: DeviceSpec) -> Any:
        raise CategorizedError(self._message, category=ErrorCategory.INFRASTRUCTURE_FAILURE)


@dataclass
class _Spy:
    """Delegates to a port and records the ledger row as the provider is polled."""

    inner: DeviceHotplugger
    dsn: str
    polls: list[tuple[str | None, bool]] = field(default_factory=list)

    def plug(self, domain_name: str, spec: DeviceSpec) -> Any:
        return self.inner.plug(domain_name, spec)

    def unplug(
        self,
        domain_name: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        return self.inner.unplug(domain_name, device_id, provider_handle, kind=kind)

    def is_attached(
        self,
        domain_name: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT provider_handle, settle_deadline IS NOT NULL FROM system_devices "
                "WHERE id = %s",
                (UUID(device_id),),
            ).fetchone()
        assert row is not None
        self.polls.append((row[0], row[1]))
        return self.inner.is_attached(domain_name, device_id, provider_handle, kind=kind)

    def release_all(self, domain_name: str) -> None:
        self.inner.release_all(domain_name)


@dataclass
class _Env:
    owner: psycopg.AsyncConnection
    worker: psycopg.AsyncConnection
    system_id: UUID
    domain: str
    inventory: FaultInjectInventory
    secrets: SecretRegistry

    def port(self, behaviour: HotplugBehaviour | None = None) -> FaultInjectHotplug:
        return FaultInjectHotplug(self.inventory, behaviour)

    async def add_row(
        self,
        state: SystemDeviceState,
        *,
        kind: str = "nic",
        provider_handle: str | None = None,
        size_gb: int | None = None,
    ) -> UUID:
        row = await SYSTEM_DEVICES.insert(
            self.owner,
            SystemDevice.model_validate(
                dict(
                    id=uuid4(),
                    created_at=_DT,
                    updated_at=_DT,
                    system_id=self.system_id,
                    kind=kind,
                    state=state,
                    size_gb=size_gb,
                    provider_handle=provider_handle,
                )
            ),
        )
        return row.id

    async def add_plugged(self, port: DeviceHotplugger, state: SystemDeviceState) -> UUID:
        """A row whose device is really in the provider, with its handle recorded."""
        device_id = await self.add_row(state)
        outcome = port.plug(
            self.domain, DeviceSpec(HotplugDeviceKind.NIC, str(device_id), None, None)
        )
        await self.owner.execute(
            "UPDATE system_devices SET provider_handle = %s WHERE id = %s",
            (outcome.provider_handle, device_id),
        )
        return device_id

    async def row(self, device_id: UUID) -> SystemDevice | None:
        return await SYSTEM_DEVICES.get(self.owner, device_id)

    async def state(self, device_id: UUID) -> SystemDeviceState:
        row = await self.row(device_id)
        assert row is not None
        return row.state

    async def leave_ready(self, state: SystemState) -> None:
        await self.owner.execute(
            "UPDATE systems SET state = %s WHERE id = %s", (state.value, self.system_id)
        )

    def job(self, kind: JobKind, device_id: UUID, **payload: object) -> Job:
        return Job(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=kind,
            payload={"system_id": str(self.system_id), "device_id": str(device_id), **payload},
            state=JobState.RUNNING,
            max_attempts=1,
            authorizing={"principal": "alice", "agent_session": None, "project": "proj"},
            dedup_key=f"{kind.value}:{device_id}",
        )

    async def plug(self, port: object, device_id: UUID, **kw: Any) -> str | None:
        return await plug_device_handler(
            self.worker,
            self.job(JobKind.PLUG_DEVICE, device_id),
            resolver=provider_resolver(hotplugger=port),
            secret_registry=self.secrets,
            window_s=_WINDOW_S,
            poll_interval_s=_POLL_S,
            **kw,
        )

    async def unplug(
        self,
        port: object,
        device_id: UUID,
        from_state: SystemDeviceState = _ATTACHED,
        kind: JobKind = JobKind.UNPLUG_DEVICE,
    ) -> str | None:
        return await unplug_device_handler(
            self.worker,
            self.job(kind, device_id, from_state=from_state.value),
            resolver=provider_resolver(hotplugger=port),
            secret_registry=self.secrets,
            window_s=_WINDOW_S,
            poll_interval_s=_POLL_S,
        )


class _ForceSpy:
    """A port that records which un-plug call the handler made and never removes the device."""

    def __init__(self, *, force_returns: bool = True) -> None:
        self.calls: list[str] = []
        self._force_returns = force_returns

    def unplug(
        self,
        domain_name: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        self.calls.append("unplug")
        return True

    def force_unplug(
        self,
        domain_name: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        self.calls.append("force_unplug")
        return self._force_returns

    def is_attached(
        self,
        domain_name: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        return True

    def release_all(self, domain_name: str) -> None:
        raise AssertionError("provider release_all must not be called")


def _run(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    scenario: Callable[[_Env], Any],
) -> None:
    @asynccontextmanager
    async def env() -> AsyncGenerator[_Env]:
        async with (
            await connect(migrated_url) as owner,
            await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_worker"), autocommit=True
            ) as worker,
        ):
            system_id = await seed_system(owner)
            domain = domain_name_for(system_id)
            inventory = FaultInjectInventory()
            inventory.record(system_id, domain)
            yield _Env(owner, worker, system_id, domain, inventory, SecretRegistry())

    async def main() -> None:
        async with env() as e:
            await scenario(e)

    asyncio.run(main())


async def _raises(awaitable: Any) -> CategorizedError:
    with pytest.raises(CategorizedError) as excinfo:
        await awaitable
    return excinfo.value


# --- plug ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "size_gb", "identity_key"), [("nic", None, "mac"), ("disk", 4, "serial")]
)
def test_plug_settles_the_row_attached(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    kind: str,
    size_gb: int | None,
    identity_key: str,
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port()
        device_id = await env.add_row(SystemDeviceState.PLUGGING, kind=kind, size_gb=size_gb)

        assert await env.plug(port, device_id) == str(device_id)

        row = await env.row(device_id)
        assert row is not None
        assert row.state is _ATTACHED
        assert set(row.identity) == {identity_key}
        assert row.provider_handle is not None
        assert (row.detail, row.settle_deadline) == (None, None)
        assert (
            port.is_attached(
                env.domain, str(device_id), row.provider_handle, kind=HotplugDeviceKind.NIC
            )
            is True
        )

    _run(migrated_url, authority_role_dsns, scenario)


def test_slow_plug_commits_handle_and_deadline_before_the_poll_settles(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        spy = _Spy(env.port(HotplugBehaviour.SLOW), migrated_url)
        device_id = await env.add_row(SystemDeviceState.PLUGGING)

        await env.plug(spy, device_id)

        assert await env.state(device_id) is _ATTACHED
        assert len(spy.polls) == 3
        assert all(handle is not None and deadline for handle, deadline in spy.polls)

    _run(migrated_url, authority_role_dsns, scenario)


def test_hang_plug_fails_with_settle_timeout_and_keeps_the_handle(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        device_id = await env.add_row(SystemDeviceState.PLUGGING)

        error = await _raises(env.plug(env.port(HotplugBehaviour.HANG), device_id))

        assert error.category is ErrorCategory.CONTROL_FAILURE
        assert error.details["reason"] == "settle_timeout"
        assert _WINDOW_S - 0.1 <= float(str(error.details["waited_s"])) < 5
        assert error.terminal is True
        row = await env.row(device_id)
        assert row is not None
        assert row.state is _FAILED
        assert row.provider_handle is not None
        assert row.detail and "settle" in row.detail
        assert row.settle_deadline is None

    _run(migrated_url, authority_role_dsns, scenario)


def test_an_injected_fault_fails_the_plug(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        engine = FaultEngine(seed=1, fault_rate={"hotplug": 1.0}, max_latency_s={})
        port = FaultedHotplug(env.port(), engine, env.inventory)
        device_id = await env.add_row(SystemDeviceState.PLUGGING)

        error = await _raises(env.plug(port, device_id))

        assert error.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        row = await env.row(device_id)
        assert row is not None and row.state is _FAILED and row.detail
        assert (
            env.port().is_attached(env.domain, str(device_id), None, kind=HotplugDeviceKind.NIC)
            is False
        )

    _run(migrated_url, authority_role_dsns, scenario)


def test_plug_retried_after_the_row_was_deleted_is_a_success(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        gone = uuid4()

        assert await env.plug(_Untouched(), gone) == str(gone)

    _run(migrated_url, authority_role_dsns, scenario)


@pytest.mark.parametrize("state", [_ATTACHED, _FAILED, SystemDeviceState.UNPLUGGING])
def test_plug_redelivered_after_its_outcome_landed_touches_nothing(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: SystemDeviceState
) -> None:
    async def scenario(env: _Env) -> None:
        device_id = await env.add_row(state)

        await env.plug(_Untouched(), device_id)

        assert await env.state(device_id) is state

    _run(migrated_url, authority_role_dsns, scenario)


def test_plug_on_a_system_that_left_ready_fails_the_row_without_a_provider_call(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        device_id = await env.add_row(SystemDeviceState.PLUGGING)
        await env.leave_ready(SystemState.TEARING_DOWN)

        error = await _raises(env.plug(_Untouched(), device_id))

        assert error.category is ErrorCategory.CONFLICT
        assert error.details["reason"] == "system_not_ready"
        row = await env.row(device_id)
        assert row is not None and row.state is _FAILED and row.detail

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_plug_the_port_rejects_as_a_configuration_error_deletes_its_row(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        port = FaultInjectHotplug(env.inventory, kinds=frozenset({HotplugDeviceKind.NIC}))
        assert HotplugDeviceKind.DISK in ADVERTISED_KINDS
        device_id = await env.add_row(SystemDeviceState.PLUGGING, kind="disk", size_gb=4)

        error = await _raises(env.plug(port, device_id))

        assert error.category is ErrorCategory.CONFIGURATION_ERROR
        assert await env.row(device_id) is None

    _run(migrated_url, authority_role_dsns, scenario)


def test_provider_text_is_redacted_before_it_reaches_detail(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        env.secrets.register("s3cr3t-value-123", scope=None)
        device_id = await env.add_row(SystemDeviceState.PLUGGING)

        await _raises(env.plug(_Failing("backend said s3cr3t-value-123 and token=abc"), device_id))

        row = await env.row(device_id)
        assert row is not None and row.detail is not None
        assert "s3cr3t-value-123" not in row.detail
        assert "abc" not in row.detail
        assert "[REDACTED]" in row.detail

    _run(migrated_url, authority_role_dsns, scenario)


def test_detail_is_bounded(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    async def scenario(env: _Env) -> None:
        device_id = await env.add_row(SystemDeviceState.PLUGGING)

        await _raises(env.plug(_Failing("x" * 5000), device_id))

        row = await env.row(device_id)
        assert row is not None and row.detail is not None and len(row.detail) == 500

    _run(migrated_url, authority_role_dsns, scenario)


# --- unplug -------------------------------------------------------------------------------


@pytest.mark.parametrize("from_state", [_ATTACHED, _FAILED, _LOST])
def test_unplug_deletes_the_row_from_any_admitted_state(
    migrated_url: str, authority_role_dsns: _RoleDsns, from_state: SystemDeviceState
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port()
        device_id = await env.add_plugged(port, SystemDeviceState.UNPLUGGING)

        assert await env.unplug(port, device_id, from_state) == str(device_id)

        assert await env.row(device_id) is None
        assert (
            port.is_attached(env.domain, str(device_id), None, kind=HotplugDeviceKind.NIC) is False
        )

    _run(migrated_url, authority_role_dsns, scenario)


def test_slow_unplug_polls_until_the_device_is_gone(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port(HotplugBehaviour.SLOW)
        device_id = await env.add_plugged(port, SystemDeviceState.UNPLUGGING)

        await env.unplug(port, device_id)

        assert await env.row(device_id) is None

    _run(migrated_url, authority_role_dsns, scenario)


def test_busy_guest_returns_the_row_to_the_state_it_started_from(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port(HotplugBehaviour.BUSY)
        device_id = await env.add_plugged(port, SystemDeviceState.UNPLUGGING)

        error = await _raises(env.unplug(port, device_id))

        assert error.category is ErrorCategory.CONTROL_FAILURE
        assert error.details["reason"] == "unplug_refused"
        row = await env.row(device_id)
        assert row is not None and row.state is _ATTACHED
        assert (row.detail, row.settle_deadline) == (None, None)
        assert (
            port.is_attached(env.domain, str(device_id), None, kind=HotplugDeviceKind.NIC) is True
        )

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_refused_unplug_of_a_failed_row_keeps_it_failed_and_says_why(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port(HotplugBehaviour.BUSY)
        device_id = await env.add_plugged(port, SystemDeviceState.UNPLUGGING)

        await _raises(env.unplug(port, device_id, _FAILED))

        row = await env.row(device_id)
        assert row is not None and row.state is _FAILED and row.detail

    _run(migrated_url, authority_role_dsns, scenario)


def test_unplug_without_a_handle_the_provider_cannot_find_is_refused(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        device_id = await env.add_row(SystemDeviceState.UNPLUGGING)

        error = await _raises(env.unplug(env.port(), device_id, _FAILED))

        assert error.details["reason"] == "handle_unknown"
        assert await env.state(device_id) is _FAILED

    _run(migrated_url, authority_role_dsns, scenario)


@pytest.mark.parametrize("kind", [HotplugDeviceKind.NIC, HotplugDeviceKind.DISK])
def test_unplug_and_its_poll_carry_the_ledger_rows_kind(
    migrated_url: str, authority_role_dsns: _RoleDsns, kind: HotplugDeviceKind
) -> None:
    seen: list[tuple[str, HotplugDeviceKind]] = []

    class _Records(_Untouched):
        def unplug(
            self,
            domain_name: str,
            device_id: str,
            provider_handle: str | None,
            *,
            kind: HotplugDeviceKind,
        ) -> bool:
            seen.append(("unplug", kind))
            return False

        def is_attached(
            self,
            domain_name: str,
            device_id: str,
            provider_handle: str | None,
            *,
            kind: HotplugDeviceKind,
        ) -> bool:
            seen.append(("is_attached", kind))
            return False

    async def scenario(env: _Env) -> None:
        size_gb = 2 if kind is HotplugDeviceKind.DISK else None
        device_id = await env.add_row(
            SystemDeviceState.UNPLUGGING, kind=kind.value, size_gb=size_gb, provider_handle="h"
        )

        await env.unplug(_Records(), device_id)

        assert seen == [("unplug", kind), ("is_attached", kind)]

    _run(migrated_url, authority_role_dsns, scenario)


def test_hang_unplug_fails_with_settle_timeout_and_the_device_stays(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port(HotplugBehaviour.HANG)
        device_id = await env.add_plugged(port, SystemDeviceState.UNPLUGGING)

        error = await _raises(env.unplug(port, device_id))

        assert error.details["reason"] == "settle_timeout"
        assert float(str(error.details["waited_s"])) >= _WINDOW_S - 0.1
        row = await env.row(device_id)
        assert row is not None and row.state is _FAILED and row.settle_deadline is None
        assert (
            port.is_attached(env.domain, str(device_id), None, kind=HotplugDeviceKind.NIC) is True
        )

    _run(migrated_url, authority_role_dsns, scenario)


def test_an_injected_fault_fails_the_unplug(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        inner = env.port()
        device_id = await env.add_plugged(inner, SystemDeviceState.UNPLUGGING)
        engine = FaultEngine(seed=1, fault_rate={"hotplug": 1.0}, max_latency_s={})

        error = await _raises(env.unplug(FaultedHotplug(inner, engine, env.inventory), device_id))

        assert error.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        row = await env.row(device_id)
        assert row is not None and row.state is _FAILED and row.detail

    _run(migrated_url, authority_role_dsns, scenario)


def test_unplug_retried_after_the_row_was_deleted_is_a_success(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        gone = uuid4()

        assert await env.unplug(_Untouched(), gone) == str(gone)

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_retried_unplug_whose_row_is_failed_proceeds(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port()
        device_id = await env.add_plugged(port, _FAILED)

        await env.unplug(port, device_id, _FAILED)

        assert await env.row(device_id) is None

    _run(migrated_url, authority_role_dsns, scenario)


def test_unplug_on_a_system_that_left_ready_returns_the_row_without_a_provider_call(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        device_id = await env.add_row(SystemDeviceState.UNPLUGGING)
        await env.leave_ready(SystemState.TEARING_DOWN)

        error = await _raises(env.unplug(_Untouched(), device_id, _LOST))

        assert error.details["reason"] == "system_not_ready"
        row = await env.row(device_id)
        assert row is not None and row.state is _LOST and row.settle_deadline is None
        assert (await SYSTEMS.get(env.owner, env.system_id)) is not None

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_configuration_error_after_the_handle_was_recorded_fails_the_row_not_deletes_it(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    class _Drifts(_Spy):
        def is_attached(
            self,
            domain_name: str,
            device_id: str,
            provider_handle: str | None,
            *,
            kind: HotplugDeviceKind,
        ) -> bool:
            raise CategorizedError("drift", category=ErrorCategory.CONFIGURATION_ERROR)

    async def scenario(env: _Env) -> None:
        device_id = await env.add_row(SystemDeviceState.PLUGGING)

        await _raises(env.plug(_Drifts(env.port(HotplugBehaviour.SLOW), migrated_url), device_id))

        row = await env.row(device_id)
        assert row is not None and row.state is _FAILED and row.provider_handle is not None

    _run(migrated_url, authority_role_dsns, scenario)


@pytest.mark.parametrize("op", ["plug", "unplug"])
def test_an_unexpected_provider_exception_fails_the_row_and_propagates(
    migrated_url: str, authority_role_dsns: _RoleDsns, op: str
) -> None:
    class _Boom(_Untouched):
        def plug(self, domain_name: str, spec: DeviceSpec) -> Any:
            raise ValueError("boom")

        def unplug(
            self,
            domain_name: str,
            device_id: str,
            provider_handle: str | None,
            *,
            kind: HotplugDeviceKind,
        ) -> bool:
            raise ValueError("boom")

    async def scenario(env: _Env) -> None:
        if op == "plug":
            device_id = await env.add_row(SystemDeviceState.PLUGGING)
            call = env.plug(_Boom(), device_id)
        else:
            device_id = await env.add_row(SystemDeviceState.UNPLUGGING)
            call = env.unplug(_Boom(), device_id)
        with pytest.raises(ValueError, match="boom"):
            await call
        row = await env.row(device_id)
        assert row is not None and row.state is _FAILED and row.detail

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_provider_timeout_with_window_left_is_not_a_settle_timeout(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    class _TimesOut(_Untouched):
        def plug(self, domain_name: str, spec: DeviceSpec) -> Any:
            raise TimeoutError("socket timed out")

    async def scenario(env: _Env) -> None:
        device_id = await env.add_row(SystemDeviceState.PLUGGING)

        with pytest.raises(TimeoutError, match="socket"):
            await env.plug(_TimesOut(), device_id)

        assert (await env.state(device_id)) is _FAILED

    _run(migrated_url, authority_role_dsns, scenario)


# --- forced unplug (ADR-0828) -------------------------------------------------------------

_FORCE = JobKind.FORCE_UNPLUG_DEVICE


@pytest.mark.parametrize(
    "behaviour", [HotplugBehaviour.BUSY, HotplugBehaviour.SLOW, HotplugBehaviour.HANG]
)
def test_forced_unplug_deletes_the_row_whatever_stalls_the_graceful_path(
    migrated_url: str, authority_role_dsns: _RoleDsns, behaviour: HotplugBehaviour
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port(behaviour)
        device_id = await env.add_plugged(port, SystemDeviceState.UNPLUGGING)

        assert await env.unplug(port, device_id, kind=_FORCE) == str(device_id)

        assert await env.row(device_id) is None
        assert (
            port.is_attached(env.domain, str(device_id), None, kind=HotplugDeviceKind.NIC) is False
        )

    _run(migrated_url, authority_role_dsns, scenario)


def test_forced_unplug_removes_a_device_already_removing(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port(HotplugBehaviour.HANG)
        device_id = await env.add_plugged(port, SystemDeviceState.UNPLUGGING)
        port.unplug(env.domain, str(device_id), None, kind=HotplugDeviceKind.NIC)

        await env.unplug(port, device_id, kind=_FORCE)

        assert await env.row(device_id) is None

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_provider_that_will_not_force_returns_the_row_to_its_prior_state(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port(HotplugBehaviour.NO_FORCE)
        device_id = await env.add_plugged(port, SystemDeviceState.UNPLUGGING)

        error = await _raises(env.unplug(port, device_id, kind=_FORCE))

        assert error.category is ErrorCategory.CONTROL_FAILURE
        assert error.details["reason"] == "unplug_refused"
        assert error.details["forced"] is True
        row = await env.row(device_id)
        assert row is not None and row.state is _ATTACHED
        assert (
            port.is_attached(env.domain, str(device_id), None, kind=HotplugDeviceKind.NIC) is True
        )

    _run(migrated_url, authority_role_dsns, scenario)


def test_forced_unplug_with_a_wrong_handle_is_refused_and_the_row_returns(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        port = env.port()
        device_id = await env.add_plugged(port, SystemDeviceState.UNPLUGGING)
        await env.owner.execute(
            "UPDATE system_devices SET provider_handle = 'another' WHERE id = %s", (device_id,)
        )

        error = await _raises(env.unplug(port, device_id, kind=_FORCE))

        assert error.details["reason"] == "handle_unknown"
        assert await env.state(device_id) is _ATTACHED

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_forced_unplug_that_never_settles_fails_the_row(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        spy = _ForceSpy(force_returns=False)
        device_id = await env.add_row(SystemDeviceState.UNPLUGGING)

        error = await _raises(env.unplug(spy, device_id, kind=_FORCE))

        assert error.details["reason"] == "settle_timeout"
        assert await env.state(device_id) is _FAILED

    _run(migrated_url, authority_role_dsns, scenario)


def test_an_injected_fault_fails_the_forced_unplug(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(env: _Env) -> None:
        inner = env.port()
        device_id = await env.add_plugged(inner, SystemDeviceState.UNPLUGGING)
        engine = FaultEngine(seed=1, fault_rate={"hotplug": 1.0}, max_latency_s={})
        port = FaultedHotplug(inner, engine, env.inventory)

        error = await _raises(env.unplug(port, device_id, kind=_FORCE))

        assert error.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert await env.state(device_id) is _FAILED

    _run(migrated_url, authority_role_dsns, scenario)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [(JobKind.UNPLUG_DEVICE, "unplug"), (JobKind.FORCE_UNPLUG_DEVICE, "force_unplug")],
)
def test_only_a_forced_job_calls_force_unplug(
    migrated_url: str, authority_role_dsns: _RoleDsns, kind: JobKind, expected: str
) -> None:
    async def scenario(env: _Env) -> None:
        spy = _ForceSpy()
        device_id = await env.add_row(SystemDeviceState.UNPLUGGING)

        await env.unplug(spy, device_id, kind=kind)

        assert spy.calls == [expected]

    _run(migrated_url, authority_role_dsns, scenario)
