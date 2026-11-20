"""A fault-inject resource row's capabilities reach the registry-built runtime (#3716)."""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.assembly import composition
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.fault_inject.capabilities import (
    FAULT_RATE_KEY,
    HOTPLUG_BEHAVIOUR_KEY,
    MAX_LATENCY_S_KEY,
    SEED_KEY,
)
from kdive.providers.ports.lifecycle import DeviceSpec, HotplugDeviceKind
from tests.providers.test_composition import _provisioning_profile

_SYSTEM_ID = UUID("33333333-3333-3333-3333-333333333333")


class _Cursor:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def execute(self, sql: object, params: tuple[UUID, ...]) -> None:
        del sql, params

    async def fetchone(self) -> dict[str, Any]:
        return self._row


class _Conn:
    """A resource row as the resolver's lookup query would return it."""

    def __init__(self, capabilities: dict[str, Any]) -> None:
        self._row = {"kind": "fault-inject", "name": "fi-1", "capabilities": capabilities}

    def cursor(self, *, row_factory: object) -> _Cursor:
        del row_factory
        return _Cursor(self._row)


def _bind(capabilities: dict[str, Any]) -> ProviderBinding:
    resolver = composition.ProviderComposition().build_provider_resolver(
        enable_fault_inject=True, enable_local_libvirt=False
    )
    conn = cast(AsyncConnection, _Conn(capabilities))
    return asyncio.run(resolver.binding_for_system(conn, _SYSTEM_ID))


def _provision(binding: ProviderBinding) -> str:
    profile: ProvisioningProfile = _provisioning_profile()
    return binding.runtime.provisioner.provision(_SYSTEM_ID, profile)


def test_row_fault_rate_fails_provisioning_through_the_registry_runtime() -> None:
    binding = _bind({SEED_KEY: 7, FAULT_RATE_KEY: {"provision": 1.0}})

    with pytest.raises(CategorizedError, match="fault-inject drew a"):
        _provision(binding)


def test_row_max_latency_is_slept_through_the_registry_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    binding = _bind({SEED_KEY: 7, MAX_LATENCY_S_KEY: {"provision": 30.0}})

    _provision(binding)

    assert slept
    assert 0.0 < slept[0] <= 30.0


def test_row_without_fault_keys_provisions_cleanly() -> None:
    assert _provision(_bind({}))


def test_row_hotplug_behaviour_busy_refuses_an_unplug() -> None:
    binding = _bind({HOTPLUG_BEHAVIOUR_KEY: "busy"})
    hotplug = binding.runtime.device_hotplug
    assert hotplug is not None
    domain = _provision(binding)
    spec = DeviceSpec(HotplugDeviceKind.NIC, "dev0", network=None, size_gb=None)
    handle = hotplug.plug(domain, spec).provider_handle

    with pytest.raises(CategorizedError) as refused:
        hotplug.unplug(domain, "dev0", handle)
    assert refused.value.category is ErrorCategory.CONTROL_FAILURE


def test_row_with_invalid_capabilities_fails_closed_at_binding() -> None:
    with pytest.raises(CategorizedError):
        _bind({FAULT_RATE_KEY: {"provision": 2.0}})
