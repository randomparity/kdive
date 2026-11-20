"""The device hot-plug capability and port on ``ProviderRuntime`` (#3698, ADR-0823)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from kdive.providers.assembly.composition import (
    build_fault_inject_runtime,
    build_local_runtime,
    build_remote_runtime,
)
from kdive.providers.core.runtime import ProviderRuntime, ProviderSupport
from kdive.providers.ports.lifecycle import DeviceHotplugger, HotplugDeviceKind
from kdive.security.secrets.secret_registry import SecretRegistry


def _bare_runtime() -> ProviderRuntime:
    port = cast(Any, object())
    return ProviderRuntime(
        profile_policy=port,
        provisioner=port,
        installer=port,
        booter=port,
        connector=port,
        controller=port,
        retriever=port,
        crash_postmortem=port,
        vmcore_introspector=port,
        live_introspector=port,
    )


def test_hotplug_kinds_are_nic_and_disk() -> None:
    assert {kind.value for kind in HotplugDeviceKind} == {"nic", "disk"}


def test_unconfigured_runtime_advertises_no_hotplug_and_wires_no_port() -> None:
    runtime = _bare_runtime()

    assert runtime.support.hotplug_devices == frozenset()
    assert runtime.device_hotplug is None


def test_runtime_rejects_advertised_hotplug_without_port() -> None:
    support = ProviderSupport(hotplug_devices=frozenset({HotplugDeviceKind.NIC}))

    with pytest.raises(ValueError, match=r"support\.hotplug_devices.*device_hotplug"):
        replace(_bare_runtime(), support=support)


def test_runtime_rejects_hotplug_port_without_advertised_kinds() -> None:
    with pytest.raises(ValueError, match=r"support\.hotplug_devices.*device_hotplug"):
        replace(_bare_runtime(), device_hotplug=cast(DeviceHotplugger, object()))


def test_runtime_accepts_advertised_hotplug_with_port() -> None:
    support = ProviderSupport(hotplug_devices=frozenset({HotplugDeviceKind.DISK}))
    port = cast(DeviceHotplugger, object())

    runtime = replace(_bare_runtime(), support=support, device_hotplug=port)

    assert runtime.support.hotplug_devices == {HotplugDeviceKind.DISK}
    assert runtime.device_hotplug is port


def test_runtime_rejects_forced_kinds_outside_the_hotplug_kinds() -> None:
    support = ProviderSupport(
        hotplug_devices=frozenset({HotplugDeviceKind.NIC}),
        forced_unplug_devices=frozenset({HotplugDeviceKind.NIC, HotplugDeviceKind.DISK}),
    )

    with pytest.raises(ValueError, match=r"forced_unplug_devices.*subset.*disk"):
        replace(_bare_runtime(), support=support, device_hotplug=cast(DeviceHotplugger, object()))


def test_runtime_accepts_forced_kinds_within_the_hotplug_kinds() -> None:
    support = ProviderSupport(
        hotplug_devices=frozenset(HotplugDeviceKind),
        forced_unplug_devices=frozenset({HotplugDeviceKind.NIC}),
    )

    runtime = replace(
        _bare_runtime(), support=support, device_hotplug=cast(DeviceHotplugger, object())
    )

    assert runtime.support.forced_unplug_devices == {HotplugDeviceKind.NIC}


def test_forced_unplug_is_not_advertised_by_default_or_by_libvirt() -> None:
    assert _bare_runtime().support.forced_unplug_devices == frozenset()
    for runtime in (
        build_local_runtime(secret_registry=SecretRegistry()),
        build_remote_runtime(secret_registry=SecretRegistry()),
    ):
        assert runtime.support.forced_unplug_devices == frozenset()


def test_libvirt_runtimes_advertise_nic_and_disk_and_wire_the_port() -> None:
    for runtime in (
        build_local_runtime(secret_registry=SecretRegistry()),
        build_remote_runtime(secret_registry=SecretRegistry()),
    ):
        assert runtime.support.hotplug_devices == frozenset(HotplugDeviceKind)
        assert runtime.device_hotplug is not None


def test_fault_inject_runtime_advertises_both_kinds_and_wires_the_port() -> None:
    runtime = build_fault_inject_runtime()

    assert runtime.support.forced_unplug_devices == frozenset(HotplugDeviceKind)
    assert runtime.support.hotplug_devices == frozenset(HotplugDeviceKind)
    assert runtime.device_hotplug is not None
