"""Tests for the local-libvirt Discovery plane (ADR-0023)."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import libvirt
import pytest

from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.resource_capabilities import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    DISK_GB_KEY,
    GUEST_ARCHES_KEY,
    PSERIES_FADUMP_KEY,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.pcie import PCIE_DEVICES_KEY
from kdive.providers.local_libvirt import discovery as discovery_module
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from tests.providers.local_libvirt.fakes import (
    FakeDomain,
    FakeLibvirtConn,
    FakeNodeDevice,
    pci_nodedev_xml,
)


def _discovery(
    conn: FakeLibvirtConn, *, cap: int = 2, fadump: bool = False
) -> LocalLibvirtDiscovery:
    # Inject a hermetic fadump probe by default so no test spawns a real qemu --version subprocess
    # (the production probe runs one when a ppc64le arch is advertised).
    return LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: conn,
        concurrent_allocation_cap=cap,
        fadump_probe=lambda _guest_arches: fadump,
    )


# An x86_64 host that can also boot ppc64le under TCG. The default FakeLibvirtConn caps XML has no
# <guest> blocks (so it exercises the empty-degradation path); this fixture is passed explicitly so
# the shared fake's default is left untouched.
_CAPS_XML_WITH_GUESTS = (
    "<capabilities><host><cpu><arch>x86_64</arch></cpu></host>"
    "<guest><os_type>hvm</os_type><arch name='x86_64'>"
    "<emulator>/usr/bin/qemu-system-x86_64</emulator>"
    "<domain type='qemu'/><domain type='kvm'/></arch></guest>"
    "<guest><os_type>hvm</os_type><arch name='ppc64le'>"
    "<emulator>/usr/bin/qemu-system-ppc64</emulator>"
    "<domain type='qemu'/></arch></guest>"
    "<guest><os_type>hvm</os_type><arch name='s390x'>"
    "<emulator>/usr/bin/qemu-system-s390x</emulator>"
    "<domain type='qemu'/></arch></guest>"
    "</capabilities>"
)


def test_list_resources_advertises_host_capabilities() -> None:
    record = _discovery(FakeLibvirtConn(), cap=3).list_resources()[0]
    assert record["resource_id"] == "qemu:///system"
    assert record["kind"] is ResourceKind.LOCAL_LIBVIRT
    assert record["status"] is ResourceStatus.AVAILABLE
    caps = record["capabilities"]
    assert caps["arch"] == "x86_64"
    assert caps["vcpus"] == 8
    assert caps["memory_mb"] == 16384
    assert caps["transports"] == ["gdbstub"]
    assert caps[CONCURRENT_ALLOCATION_CAP_KEY] == 3


def test_list_resources_advertises_disk_ceiling_from_host_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    # discovery reads only `.total`; a SimpleNamespace suffices (avoid private shutil types).
    monkeypatch.setattr(
        discovery_module.shutil,
        "disk_usage",
        lambda _path: types.SimpleNamespace(total=200 * 1024**3, used=0, free=200 * 1024**3),
    )
    caps = _discovery(FakeLibvirtConn()).list_resources()[0]["capabilities"]
    assert caps[DISK_GB_KEY] == 200


def test_disk_ceiling_unstattable_is_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_path: object) -> object:
        raise OSError("no such path")

    monkeypatch.setattr(discovery_module.shutil, "disk_usage", _raise)
    with pytest.raises(CategorizedError) as exc:
        _discovery(FakeLibvirtConn()).list_resources()
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_list_resources_advertises_guest_arches_filtered_to_supported() -> None:
    conn = FakeLibvirtConn(caps_xml=_CAPS_XML_WITH_GUESTS)
    caps = _discovery(conn).list_resources()[0]["capabilities"]
    # s390x is bootable but not kdive-provisionable, so it is dropped; native x86_64 -> kvm,
    # foreign ppc64le -> tcg.
    assert caps[GUEST_ARCHES_KEY] == {
        "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"},
        "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"},
    }


def test_list_resources_records_pseries_fadump_from_the_probe() -> None:
    # The probe's verdict (keyed off the discovered ppc64le emulator, ADR-0349) is recorded on the
    # capabilities column, so admission can gate a fadump-opted provision against it.
    conn = FakeLibvirtConn(caps_xml=_CAPS_XML_WITH_GUESTS)
    supported = _discovery(conn, fadump=True).list_resources()[0]["capabilities"]
    assert supported[PSERIES_FADUMP_KEY] is True
    unsupported = _discovery(conn, fadump=False).list_resources()[0]["capabilities"]
    assert unsupported[PSERIES_FADUMP_KEY] is False


def test_list_resources_probe_receives_the_parsed_guest_arches() -> None:
    # The probe is fed the same guest_arches recorded on the row, so it reads the ppc64le emulator.
    seen: dict[str, Mapping[str, Mapping[str, str]]] = {}

    def _probe(guest_arches: Mapping[str, Mapping[str, str]]) -> bool:
        seen["arches"] = guest_arches
        return False

    conn = FakeLibvirtConn(caps_xml=_CAPS_XML_WITH_GUESTS)
    LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: conn,
        concurrent_allocation_cap=2,
        fadump_probe=_probe,
    ).list_resources()
    assert seen["arches"]["ppc64le"]["emulator"] == "/usr/bin/qemu-system-ppc64"


def test_list_resources_guest_arches_empty_when_no_guest_blocks() -> None:
    # The default fake caps XML has only <host><cpu><arch>; a host advertising no <guest> blocks
    # yields an empty mapping and does not break the other advertised capabilities.
    caps = _discovery(FakeLibvirtConn()).list_resources()[0]["capabilities"]
    assert caps[GUEST_ARCHES_KEY] == {}
    assert caps["arch"] == "x86_64"


def test_list_resources_arch_unknown_when_absent() -> None:
    conn = FakeLibvirtConn(caps_xml="<capabilities><host></host></capabilities>")
    record = _discovery(conn).list_resources()[0]
    assert record["capabilities"]["arch"] == "unknown"


def test_list_resources_empty_pcie_devices_when_no_nodedev() -> None:
    caps = _discovery(FakeLibvirtConn()).list_resources()[0]["capabilities"]
    assert caps[PCIE_DEVICES_KEY] == []


def test_list_resources_populates_pcie_descriptors() -> None:
    conn = FakeLibvirtConn(
        node_devices=[
            FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml()),
            FakeNodeDevice(
                "pci_0000_00_1f_0",
                pci_nodedev_xml(
                    name="pci_0000_00_1f_0",
                    cls="0x060100",
                    bus=0,
                    slot=31,  # decimal 31 == hex 1f
                    function=0,
                    vendor_id="0x8086",
                    device_id="0x7a8a",
                    product_label=None,  # self-closing <product/>, no text
                ),
            ),
        ]
    )
    devices = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY]
    assert devices[0] == {
        "bdf": "0000:3b:00.0",
        "vendor_id": "8086",
        "device_id": "1572",
        "class_code": "020000",
        "label": "Ethernet Controller X710",
    }
    # Decimal slot 31 → hex 1f in the bdf; empty product text falls back to vendor:device.
    assert devices[1]["bdf"] == "0000:00:1f.0"
    assert devices[1]["device_id"] == "7a8a"
    assert devices[1]["class_code"] == "060100"
    assert devices[1]["label"] == "8086:7a8a"


def test_compose_bdf_uses_all_address_fields() -> None:
    # Distinct non-zero domain/bus/slot/function (decimal) so a wrong tag or dropped
    # default cannot coincidentally produce the right hex BDF.
    conn = FakeLibvirtConn(
        node_devices=[
            FakeNodeDevice(
                "pci_0001_0b_16_3",
                pci_nodedev_xml(domain=1, bus=11, slot=22, function=3),
            )
        ]
    )
    device = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY][0]
    assert device["bdf"] == "0001:0b:16.3"


def test_compose_bdf_defaults_missing_address_fields_to_zero() -> None:
    # A PCI capability missing the address elements must default each to 0 (BDF 0000:00:00.0),
    # not raise — the int(default="0") fallbacks.
    xml = (
        "<device><name>pci_min</name>"
        "<capability type='pci'>"
        "<class>0x020000</class>"
        "<product id='0x1572'>NIC</product>"
        "<vendor id='0x8086'>Intel</vendor>"
        "</capability></device>"
    )
    conn = FakeLibvirtConn(node_devices=[FakeNodeDevice("pci_min", xml)])
    device = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY][0]
    assert device["bdf"] == "0000:00:00.0"


def test_class_code_is_lowercased() -> None:
    conn = FakeLibvirtConn(
        node_devices=[FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml(cls="0x0A0BCD"))]
    )
    device = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY][0]
    assert device["class_code"] == "0a0bcd"


def test_pci_descriptor_without_class_is_skipped() -> None:
    # No <class> element → empty class_code → descriptor dropped (not coerced to a placeholder).
    xml = (
        "<device><name>pci_noclass</name>"
        "<capability type='pci'>"
        "<domain>0</domain><bus>59</bus><slot>0</slot><function>0</function>"
        "<product id='0x1572'>NIC</product>"
        "<vendor id='0x8086'>Intel</vendor>"
        "</capability></device>"
    )
    conn = FakeLibvirtConn(
        node_devices=[
            FakeNodeDevice("pci_noclass", xml),
            FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml()),
        ]
    )
    devices = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY]
    assert [d["bdf"] for d in devices] == ["0000:3b:00.0"]


def test_list_pcie_requests_only_pci_devices() -> None:
    # The node-device enumeration must pass the PCI-device filter flag, not a placeholder.
    recorded_flags: list[object] = []

    class _RecordingConn(FakeLibvirtConn):
        def listAllDevices(  # noqa: N802 - mirrors the libvirt binding name
            self, flags: int = 0
        ) -> list[FakeNodeDevice]:
            recorded_flags.append(flags)
            return list(self.node_devices)

    conn = _RecordingConn(node_devices=[FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml())])
    _discovery(conn).list_resources()
    assert recorded_flags == [libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_PCI_DEV]


def test_pcie_descriptor_has_no_free_flag() -> None:
    conn = FakeLibvirtConn(node_devices=[FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml())])
    descriptor = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY][0]
    assert "free" not in descriptor


def test_malformed_nodedev_is_skipped_not_fatal() -> None:
    conn = FakeLibvirtConn(
        node_devices=[
            FakeNodeDevice("broken", "<device><name>broken</name></device>"),  # no pci capability
            FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml()),
        ]
    )
    devices = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY]
    assert [d["bdf"] for d in devices] == ["0000:3b:00.0"]


def test_unparseable_nodedev_logs_device_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = FakeLibvirtConn(
        node_devices=[
            FakeNodeDevice("bad-pci", "<device><name>bad-pci</name>"),
            FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml()),
        ]
    )
    caplog.set_level(logging.WARNING, logger="kdive.providers.local_libvirt.discovery")
    devices = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY]
    assert [d["bdf"] for d in devices] == ["0000:3b:00.0"]
    assert any(
        record.exc_info is not None
        and "bad-pci" in record.message
        and "unparseable PCI node-device XML" in record.message
        for record in caplog.records
    )


def test_list_owned_returns_only_tagged_domains() -> None:
    conn = FakeLibvirtConn(
        domains=[
            FakeDomain("kdive-1", system_id="11111111-1111-1111-1111-111111111111"),
            FakeDomain("other-vm", system_id=None),  # untagged → skipped
        ]
    )
    owned = _discovery(conn).list_owned()
    assert owned == [
        {"system_id": "11111111-1111-1111-1111-111111111111", "domain_name": "kdive-1"}
    ]


def test_list_owned_surfaces_convention_named_untagged_orphan() -> None:
    # #372: a kdive-<uuid> domain whose metadata tag is gone (VIR_ERR_NO_DOMAIN_METADATA) is
    # still ours by naming convention; surface it with an empty system_id so the reconciler
    # falls back to the name. A non-convention untagged domain is still skipped.
    conn = FakeLibvirtConn(
        domains=[
            FakeDomain("kdive-22222222-2222-2222-2222-222222222222", system_id=None),
            FakeDomain("kdive-3", system_id="33333333-3333-3333-3333-333333333333"),
            FakeDomain("other-vm", system_id=None),  # non-convention untagged → skipped
        ]
    )
    owned = _discovery(conn).list_owned()
    assert owned == [
        {"system_id": "", "domain_name": "kdive-22222222-2222-2222-2222-222222222222"},
        {"system_id": "33333333-3333-3333-3333-333333333333", "domain_name": "kdive-3"},
    ]


def test_list_owned_reraises_non_metadata_libvirt_error() -> None:
    from kdive.domain.errors import ErrorCategory

    conn = FakeLibvirtConn(
        domains=[FakeDomain("vm", system_id=None, raise_code=libvirt.VIR_ERR_INTERNAL_ERROR)]
    )
    with pytest.raises(CategorizedError) as exc:
        _discovery(conn).list_owned()

    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details == {"domain": "vm"}
    assert str(exc.value) == "libvirt error reading domain metadata"


def test_from_env_reads_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    monkeypatch.setenv("KDIVE_LIBVIRT_ALLOCATION_CAP", "4")
    disc = LocalLibvirtDiscovery.from_env()
    assert disc.concurrent_allocation_cap == 4
    assert disc.host_uri == "qemu:///system"


def test_from_env_defaults_cap_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_ALLOCATION_CAP", raising=False)
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    assert LocalLibvirtDiscovery.from_env().concurrent_allocation_cap == 1


def test_from_env_non_int_cap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from kdive.domain.errors import ErrorCategory

    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    monkeypatch.setenv("KDIVE_LIBVIRT_ALLOCATION_CAP", "lots")
    with pytest.raises(CategorizedError) as exc:
        LocalLibvirtDiscovery.from_env()

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "is not an integer" in str(exc.value)
    assert "lots" in str(exc.value)


def test_from_env_connect_opens_configured_host_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    import kdive.providers.local_libvirt.discovery as discovery_module

    opened: list[object] = []
    sentinel = object()

    def _fake_open(uri: object) -> object:
        opened.append(uri)
        return sentinel

    monkeypatch.setattr(discovery_module.libvirt, "open", _fake_open)
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu+ssh://host/system")
    monkeypatch.setenv("KDIVE_LIBVIRT_ALLOCATION_CAP", "2")

    disc = LocalLibvirtDiscovery.from_env()
    conn = disc._connect()

    assert conn is sentinel
    assert opened == ["qemu+ssh://host/system"]
