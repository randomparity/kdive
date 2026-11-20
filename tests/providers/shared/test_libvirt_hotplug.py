"""LibvirtDeviceHotplug against a stateful fake libvirt domain (#3687, ADR-0823)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.lifecycle import DeviceSpec, HotplugDeviceKind
from kdive.providers.shared.libvirt_hotplug import (
    LibvirtDeviceHotplug,
    NicSource,
    device_alias,
    disk_serial,
    free_disk_target,
    nic_mac,
    render_disk_xml,
    render_nic_xml,
)

DOMAIN = "kdive-test"


def _el(root: ET.Element, path: str) -> ET.Element:
    found = root.find(path)
    assert found is not None, path
    return found


BOOT_XML = (
    "<domain><devices><disk type='file' device='disk'><target dev='vda' bus='virtio'/></disk>"
    "<interface type='user'><mac address='52:54:00:00:00:01'/></interface></devices></domain>"
)


class _FakeError(libvirt.libvirtError):
    def __init__(self, code: int, message: str = "fake") -> None:
        self._code = code
        super().__init__(message)

    def get_error_code(self) -> int:
        return self._code

    def get_error_message(self) -> str:
        return str(self)


class _Domain:
    def __init__(self, *, active: bool = True) -> None:
        self.active = active
        self.devices: list[ET.Element] = []
        self.attach_error: libvirt.libvirtError | None = None
        self.detach_error: libvirt.libvirtError | None = None
        self.guest_releases = True  # detach completes inside the call
        self.pending: list[ET.Element] = []
        self.attached_xml: list[str] = []

    def isActive(self) -> int:  # noqa: N802
        return int(self.active)

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        root = ET.fromstring(BOOT_XML)
        devices = root.find("devices")
        assert devices is not None
        devices.extend(self.devices)
        return ET.tostring(root, encoding="unicode")

    def attachDeviceFlags(self, xml: str, flags: int) -> int:  # noqa: N802
        assert flags == libvirt.VIR_DOMAIN_AFFECT_LIVE
        if self.attach_error is not None:
            raise self.attach_error
        self.attached_xml.append(xml)
        self.devices.append(ET.fromstring(xml))
        return 0

    def detachDeviceFlags(self, xml: str, flags: int) -> int:  # noqa: N802
        assert flags == libvirt.VIR_DOMAIN_AFFECT_LIVE
        if self.detach_error is not None:
            raise self.detach_error
        alias = ET.fromstring(xml).find("alias")
        assert alias is not None
        if self.guest_releases:
            self.devices = [
                d
                for d in self.devices
                if d.find("alias") is None or _el(d, "alias").get("name") != alias.get("name")
            ]
        return 0

    def finish_release(self) -> None:
        self.devices = []


class _Conn:
    def __init__(self, domain: _Domain | None) -> None:
        self.domain = domain

    def lookupByName(self, name: str) -> _Domain:  # noqa: N802
        if self.domain is None:
            raise _FakeError(libvirt.VIR_ERR_NO_DOMAIN)
        return self.domain


class _Volumes:
    def __init__(self) -> None:
        self.live: dict[str, str] = {}
        self.removed_all: list[str] = []
        self.removes: list[str] = []

    def create(self, conn: Any, domain_name: str, device_id: str, size_gb: int, /) -> str:
        self.live[device_id] = f"/pool/{device_id}.qcow2"
        return self.live[device_id]

    def remove(self, conn: Any, domain_name: str, device_id: str, /) -> None:
        self.removes.append(device_id)
        self.live.pop(device_id, None)

    def remove_all(self, conn: Any, domain_name: str, /) -> None:
        self.removed_all.append(domain_name)
        self.live.clear()


def _port(
    domain: _Domain | None, volumes: _Volumes, *, user_mode: bool = True
) -> LibvirtDeviceHotplug:
    conn = _Conn(domain)

    @contextmanager
    def connection() -> Generator[_Conn]:
        yield conn

    def nic_source(network: str | None) -> NicSource:
        return ("user", None) if user_mode and network is None else ("network", network or "dflt")

    return LibvirtDeviceHotplug(
        connection=connection,
        volumes=volumes,
        nic_source=nic_source,
        kinds=frozenset(HotplugDeviceKind),
    )


def _nic(device_id: str, network: str | None = None) -> DeviceSpec:
    return DeviceSpec(HotplugDeviceKind.NIC, device_id, network, None)


def _disk(device_id: str, size_gb: int = 2) -> DeviceSpec:
    return DeviceSpec(HotplugDeviceKind.DISK, device_id, None, size_gb)


def test_plug_nic_attaches_live_with_deterministic_mac_and_alias() -> None:
    domain, device_id = _Domain(), str(uuid4())
    outcome = _port(domain, _Volumes()).plug(DOMAIN, _nic(device_id))

    assert outcome.settled is True
    assert outcome.provider_handle == device_alias(device_id)
    assert outcome.identity == {"mac": nic_mac(device_id)}
    assert f"ua-kdive-{device_id}" in domain.attached_xml[0]


def test_plug_disk_creates_volume_and_attaches_on_first_free_target() -> None:
    domain, volumes, device_id = _Domain(), _Volumes(), str(uuid4())
    outcome = _port(domain, volumes).plug(DOMAIN, _disk(device_id))

    assert outcome.identity == {"serial": disk_serial(device_id)}
    assert len(disk_serial(device_id)) <= 20
    assert volumes.live == {device_id: f"/pool/{device_id}.qcow2"}
    attached = ET.fromstring(domain.attached_xml[0])
    assert _el(attached, "target").get("dev") == "vdb"
    assert _el(attached, "driver").get("type") == "qcow2"


def test_plug_is_idempotent_on_an_attached_device() -> None:
    domain, volumes, device_id = _Domain(), _Volumes(), str(uuid4())
    port = _port(domain, volumes)
    first = port.plug(DOMAIN, _disk(device_id))
    second = port.plug(DOMAIN, _disk(device_id))

    assert second == first
    assert len(domain.attached_xml) == 1


def test_second_disk_takes_the_next_target() -> None:
    domain, volumes = _Domain(), _Volumes()
    port = _port(domain, volumes)
    port.plug(DOMAIN, _disk(str(uuid4())))
    port.plug(DOMAIN, _disk(str(uuid4())))

    targets = [_el(ET.fromstring(x), "target").get("dev") for x in domain.attached_xml]
    assert targets == ["vdb", "vdc"]


def test_unknown_network_is_configuration_error_with_reason() -> None:
    domain = _Domain()
    domain.attach_error = _FakeError(libvirt.VIR_ERR_NO_NETWORK)

    with pytest.raises(CategorizedError) as err:
        _port(domain, _Volumes()).plug(DOMAIN, _nic(str(uuid4()), "nonet"))

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.value.details["reason"] == "unknown_network"


def test_failed_disk_attach_frees_the_new_volume() -> None:
    domain, volumes, device_id = _Domain(), _Volumes(), str(uuid4())
    domain.attach_error = _FakeError(libvirt.VIR_ERR_INTERNAL_ERROR, "No more available PCI slots")

    with pytest.raises(CategorizedError) as err:
        _port(domain, volumes).plug(DOMAIN, _disk(device_id))

    assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "PCI slots" in str(err.value.details["libvirt_error"])
    assert volumes.live == {}


def test_hypervisor_refusal_of_an_attach_is_a_control_failure() -> None:
    domain, volumes = _Domain(), _Volumes()
    domain.attach_error = _FakeError(
        libvirt.VIR_ERR_OPERATION_FAILED, "No more available PCI slots"
    )

    with pytest.raises(CategorizedError) as err:
        _port(domain, volumes).plug(DOMAIN, _disk(str(uuid4())))

    assert err.value.category is ErrorCategory.CONTROL_FAILURE
    assert err.value.details["reason"] == "plug_refused"
    assert volumes.live == {}


def test_plug_into_stopped_domain_is_domain_not_running() -> None:
    with pytest.raises(CategorizedError) as err:
        _port(_Domain(active=False), _Volumes()).plug(DOMAIN, _nic(str(uuid4())))

    assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert err.value.details["reason"] == "domain_not_running"


def test_plug_into_absent_domain_is_domain_absent() -> None:
    with pytest.raises(CategorizedError) as err:
        _port(None, _Volumes()).plug(DOMAIN, _nic(str(uuid4())))

    assert err.value.details["reason"] == "domain_absent"


@pytest.mark.parametrize(
    "spec",
    [
        DeviceSpec(HotplugDeviceKind.NIC, "not-a-uuid", None, None),
        DeviceSpec(HotplugDeviceKind.DISK, str(uuid4()), None, None),
        DeviceSpec(HotplugDeviceKind.NIC, str(uuid4()), "a b", None),
    ],
)
def test_plug_rejects_malformed_input_before_any_libvirt_call(spec: DeviceSpec) -> None:
    domain = _Domain()
    with pytest.raises(CategorizedError) as err:
        _port(domain, _Volumes()).plug(DOMAIN, spec)

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert domain.attached_xml == []


def test_kind_outside_the_advertised_set_is_refused() -> None:
    port = LibvirtDeviceHotplug(
        connection=lambda: _port(_Domain(), _Volumes())._connection(),  # noqa: SLF001
        volumes=_Volumes(),
        nic_source=lambda _n: ("user", None),
        kinds=frozenset({HotplugDeviceKind.NIC}),
    )
    with pytest.raises(CategorizedError) as err:
        port.plug(DOMAIN, _disk(str(uuid4())))

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_unplug_removes_device_and_frees_volume_when_guest_releases() -> None:
    domain, volumes, device_id = _Domain(), _Volumes(), str(uuid4())
    port = _port(domain, volumes)
    handle = port.plug(DOMAIN, _disk(device_id)).provider_handle

    assert port.unplug(DOMAIN, device_id, handle, kind=HotplugDeviceKind.DISK) is True
    assert volumes.live == {}
    assert port.is_attached(DOMAIN, device_id, handle, kind=HotplugDeviceKind.DISK) is False


def test_unplug_unsettled_then_is_attached_frees_volume_on_first_absent_look() -> None:
    domain, volumes, device_id = _Domain(), _Volumes(), str(uuid4())
    port = _port(domain, volumes)
    handle = port.plug(DOMAIN, _disk(device_id)).provider_handle
    domain.guest_releases = False

    assert port.unplug(DOMAIN, device_id, handle, kind=HotplugDeviceKind.DISK) is False
    assert port.is_attached(DOMAIN, device_id, handle, kind=HotplugDeviceKind.DISK) is True
    assert volumes.live != {}

    domain.finish_release()
    assert port.is_attached(DOMAIN, device_id, handle, kind=HotplugDeviceKind.DISK) is False
    assert volumes.live == {}


def test_unplug_of_absent_device_is_true_and_frees_a_leftover_volume() -> None:
    volumes, device_id = _Volumes(), str(uuid4())
    volumes.live[device_id] = "/pool/x"

    assert (
        _port(_Domain(), volumes).unplug(DOMAIN, device_id, None, kind=HotplugDeviceKind.DISK)
        is True
    )
    assert volumes.live == {}


def test_nic_unplug_makes_no_volume_call_when_the_guest_releases() -> None:
    domain, volumes, device_id = _Domain(), _Volumes(), str(uuid4())
    port = _port(domain, volumes)
    handle = port.plug(DOMAIN, _nic(device_id)).provider_handle

    assert port.unplug(DOMAIN, device_id, handle, kind=HotplugDeviceKind.NIC) is True
    assert volumes.removes == []


def test_nic_unplug_then_is_attached_makes_no_volume_call() -> None:
    domain, volumes, device_id = _Domain(), _Volumes(), str(uuid4())
    port = _port(domain, volumes)
    handle = port.plug(DOMAIN, _nic(device_id)).provider_handle
    domain.guest_releases = False
    nic = HotplugDeviceKind.NIC

    assert port.unplug(DOMAIN, device_id, handle, kind=nic) is False
    domain.finish_release()
    assert port.is_attached(DOMAIN, device_id, handle, kind=nic) is False
    assert volumes.removes == []


def test_nic_absent_unplug_and_is_attached_make_no_volume_call() -> None:
    volumes, device_id = _Volumes(), str(uuid4())
    port = _port(_Domain(), volumes)

    assert port.unplug(DOMAIN, device_id, None, kind=HotplugDeviceKind.NIC) is True
    assert port.is_attached(DOMAIN, device_id, None, kind=HotplugDeviceKind.NIC) is False
    assert volumes.removes == []


def test_disk_absent_unplug_and_is_attached_each_free_the_volume_once() -> None:
    volumes, device_id = _Volumes(), str(uuid4())
    port = _port(_Domain(), volumes)

    assert port.unplug(DOMAIN, device_id, None, kind=HotplugDeviceKind.DISK) is True
    assert volumes.removes == [device_id]
    assert port.is_attached(DOMAIN, device_id, None, kind=HotplugDeviceKind.DISK) is False
    assert volumes.removes == [device_id, device_id]


def test_disk_unplug_that_the_guest_releases_frees_the_volume_once() -> None:
    domain, volumes, device_id = _Domain(), _Volumes(), str(uuid4())
    port = _port(domain, volumes)
    handle = port.plug(DOMAIN, _disk(device_id)).provider_handle

    assert port.unplug(DOMAIN, device_id, handle, kind=HotplugDeviceKind.DISK) is True
    assert volumes.removes == [device_id]


def test_unplug_of_absent_device_on_stopped_domain_is_true() -> None:
    assert (
        _port(_Domain(active=False), _Volumes()).unplug(
            DOMAIN, str(uuid4()), None, kind=HotplugDeviceKind.DISK
        )
        is True
    )


def test_unplug_refused_by_guest_is_control_failure() -> None:
    domain, device_id = _Domain(), str(uuid4())
    port = _port(domain, _Volumes())
    handle = port.plug(DOMAIN, _nic(device_id)).provider_handle
    domain.detach_error = _FakeError(libvirt.VIR_ERR_OPERATION_FAILED, "device busy")

    with pytest.raises(CategorizedError) as err:
        port.unplug(DOMAIN, device_id, handle, kind=HotplugDeviceKind.DISK)

    assert err.value.category is ErrorCategory.CONTROL_FAILURE
    assert err.value.details["reason"] == "unplug_refused"
    assert len(domain.devices) == 1


def test_repeated_unplug_while_the_guest_has_not_released_is_still_pending() -> None:
    domain, device_id = _Domain(), str(uuid4())
    port = _port(domain, _Volumes())
    handle = port.plug(DOMAIN, _nic(device_id)).provider_handle
    domain.guest_releases = False
    assert port.unplug(DOMAIN, device_id, handle, kind=HotplugDeviceKind.DISK) is False
    domain.detach_error = _FakeError(
        libvirt.VIR_ERR_OPERATION_INVALID, "device is already in the process of unplug"
    )

    assert port.unplug(DOMAIN, device_id, handle, kind=HotplugDeviceKind.DISK) is False


def test_unplug_libvirt_fault_is_infrastructure_failure() -> None:
    domain, device_id = _Domain(), str(uuid4())
    port = _port(domain, _Volumes())
    port.plug(DOMAIN, _nic(device_id))
    domain.detach_error = _FakeError(libvirt.VIR_ERR_INTERNAL_ERROR)

    with pytest.raises(CategorizedError) as err:
        port.unplug(DOMAIN, device_id, None, kind=HotplugDeviceKind.DISK)

    assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_foreign_handle_is_handle_unknown() -> None:
    with pytest.raises(CategorizedError) as err:
        _port(_Domain(), _Volumes()).unplug(
            DOMAIN, str(uuid4()), "ua-kdive-someone-else", kind=HotplugDeviceKind.DISK
        )

    assert err.value.category is ErrorCategory.CONTROL_FAILURE
    assert err.value.details["reason"] == "handle_unknown"


def test_boot_disk_and_ssh_nic_are_never_matched() -> None:
    domain = _Domain()
    port = _port(domain, _Volumes())

    assert port.is_attached(DOMAIN, str(uuid4()), None, kind=HotplugDeviceKind.DISK) is False
    assert port.unplug(DOMAIN, str(uuid4()), None, kind=HotplugDeviceKind.DISK) is True
    assert len(domain.XMLDesc()) > 0


def test_release_all_deletes_every_device_volume() -> None:
    volumes = _Volumes()
    port = _port(_Domain(), volumes)
    port.plug(DOMAIN, _disk(str(uuid4())))

    port.release_all(DOMAIN)

    assert volumes.removed_all == [DOMAIN]
    assert volumes.live == {}


def test_rendered_xml_carries_alias_and_identity() -> None:
    device_id = str(uuid4())
    nic = ET.fromstring(render_nic_xml(device_id, ("network", "dflt")))
    disk = ET.fromstring(render_disk_xml(device_id, path="/p/d.qcow2", target="vdz"))

    assert _el(nic, "source").get("network") == "dflt"
    assert _el(nic, "mac").get("address") == nic_mac(device_id)
    assert _el(disk, "serial").text == disk_serial(device_id)
    assert _el(disk, "alias").get("name") == device_alias(device_id)


def test_free_disk_target_exhaustion_is_a_refusal() -> None:
    targets = "".join(f"<disk><target dev='vd{c}'/></disk>" for c in "abcdefghijklmnopqrstuvwxyz")
    with pytest.raises(CategorizedError) as err:
        free_disk_target(f"<domain><devices>{targets}</devices></domain>", domain_name=DOMAIN)

    assert err.value.details["reason"] == "no_free_target"
