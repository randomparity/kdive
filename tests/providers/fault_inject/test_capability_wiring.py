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
from kdive.providers.fault_inject.lifecycle import faulted
from kdive.providers.ports.lifecycle import DeviceSpec, HotplugDeviceKind

_SYSTEM_ID = UUID("33333333-3333-3333-3333-333333333333")


def _provisioning_profile() -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 1,
            "memory_mb": 1024,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "provider": {
                "local-libvirt": {
                    "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/x.qcow2"},
                }
            },
        }
    )


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


def test_row_max_latency_is_drawn_through_the_registry_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drawn: list[float] = []
    monkeypatch.setattr(
        faulted, "_apply", lambda decision, _sleep: drawn.append(decision.latency_s)
    )
    binding = _bind({SEED_KEY: 7, MAX_LATENCY_S_KEY: {"provision": 30.0}})

    _provision(binding)

    assert len(drawn) == 1
    assert 0.0 < drawn[0] <= 30.0


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


def test_hotplug_ledger_survives_a_rebind_with_changed_behaviour() -> None:
    resolver = composition.ProviderComposition().build_provider_resolver(
        enable_fault_inject=True, enable_local_libvirt=False
    )
    spec = DeviceSpec(HotplugDeviceKind.NIC, "dev0", network=None, size_gb=None)

    def bind(capabilities: dict[str, Any]) -> ProviderBinding:
        conn = cast(AsyncConnection, _Conn(capabilities))
        return asyncio.run(resolver.binding_for_system(conn, _SYSTEM_ID))

    first = bind({})
    domain = _provision(first)
    assert first.runtime.device_hotplug is not None
    handle = first.runtime.device_hotplug.plug(domain, spec).provider_handle

    busy = bind({HOTPLUG_BEHAVIOUR_KEY: "busy"}).runtime.device_hotplug
    assert busy is not None
    with pytest.raises(CategorizedError):
        busy.unplug(domain, "dev0", handle)
    assert busy.is_attached(domain, "dev0", handle) is True


def test_build_time_behaviour_is_the_default_for_a_row_without_the_key() -> None:
    from kdive.providers.fault_inject.lifecycle.hotplug import HotplugBehaviour

    runtime = composition.build_fault_inject_runtime(hotplug_behaviour=HotplugBehaviour.BUSY)
    bound = runtime.for_resource("fi-1", {})

    assert bound.device_hotplug is not None
    domain = bound.provisioner.provision(_SYSTEM_ID, _provisioning_profile())
    spec = DeviceSpec(HotplugDeviceKind.NIC, "dev0", network=None, size_gb=None)
    handle = bound.device_hotplug.plug(domain, spec).provider_handle
    with pytest.raises(CategorizedError):
        bound.device_hotplug.unplug(domain, "dev0", handle)


@pytest.mark.parametrize(
    "capabilities",
    [
        *(
            {key: value}
            for key in (FAULT_RATE_KEY, MAX_LATENCY_S_KEY)
            for value in (0, False, "", [])
        ),
        {FAULT_RATE_KEY: {"provision": 2.0}},
        {SEED_KEY: "7"},
        {SEED_KEY: None},
        {HOTPLUG_BEHAVIOUR_KEY: "explode"},
    ],
)
def test_row_with_a_malformed_or_falsy_key_fails_closed_at_binding(
    capabilities: dict[str, Any],
) -> None:
    with pytest.raises(CategorizedError) as raised:
        _bind(capabilities)
    assert raised.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "capabilities",
    [{}, {SEED_KEY: 7}, {FAULT_RATE_KEY: {}, MAX_LATENCY_S_KEY: {}}, {FAULT_RATE_KEY: None}],
)
def test_row_with_no_fault_configured_binds_without_wrappers(
    capabilities: dict[str, Any],
) -> None:
    assert _provision(_bind(capabilities))
