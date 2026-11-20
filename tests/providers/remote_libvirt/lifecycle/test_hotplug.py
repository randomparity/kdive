"""Remote-libvirt hot-plug seams: pool volumes, owner identity, NIC network (#3687, ADR-0823)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.lifecycle import HotplugDeviceKind
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.lifecycle.hotplug import (
    RemoteDeviceVolumes,
    render_scratch_volume_xml,
    scratch_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name
from kdive.providers.shared.libvirt_hotplug import LibvirtDeviceHotplug


def _el(root: ET.Element, path: str) -> ET.Element:
    found = root.find(path)
    assert found is not None, path
    return found


SYSTEM = UUID("22222222-2222-2222-2222-222222222222")
DOMAIN = f"kdive-{SYSTEM}"


class _Err(libvirt.libvirtError):
    def __init__(self, code: int) -> None:
        self._code = code
        super().__init__("fake")

    def get_error_code(self) -> int:
        return self._code


class _Volume:
    def __init__(self, pool: _Pool, name: str, xml: str = "") -> None:
        self._pool, self._name, self._xml = pool, name, xml

    def delete(self, flags: int = 0) -> int:
        del self._pool.volumes[self._name]
        return 0

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        return self._xml

    def path(self) -> str:
        return f"/pool/{self._name}"


class _Pool:
    def __init__(self) -> None:
        self.volumes: dict[str, _Volume] = {}
        self.created_xml: list[str] = []
        self.refreshed = 0

    def refresh(self, flags: int = 0) -> int:
        self.refreshed += 1
        return 0

    def listVolumes(self) -> list[str]:  # noqa: N802
        return list(self.volumes)

    def storageVolLookupByName(self, name: str) -> _Volume:  # noqa: N802
        if name not in self.volumes:
            raise _Err(libvirt.VIR_ERR_NO_STORAGE_VOL)
        return self.volumes[name]

    def createXML(self, xml: str, flags: int = 0) -> _Volume:  # noqa: N802
        self.created_xml.append(xml)
        name = ET.fromstring(xml).findtext("name", "")
        self.volumes[name] = _Volume(self, name)
        return self.volumes[name]


class _Conn:
    def __init__(self, pool: _Pool | None) -> None:
        self._pool = pool

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        if self._pool is None:
            raise _Err(libvirt.VIR_ERR_NO_STORAGE_POOL)
        return self._pool


def _volumes() -> RemoteDeviceVolumes:
    config = RemoteLibvirtConfig(
        uri="qemu+tls://host/system",
        cert_refs=TlsCertRefs("c", "k", "ca"),
        concurrent_allocation_cap=1,
        storage_pool="images",
    )
    return RemoteDeviceVolumes(lambda: config)


def _overlay_xml(owner: int, group: int) -> str:
    return (
        "<volume><target><permissions>"
        f"<owner>{owner}</owner><group>{group}</group></permissions></target></volume>"
    )


def test_create_copies_the_overlay_owner_and_returns_the_pool_path() -> None:
    pool, device_id = _Pool(), str(uuid4())
    pool.volumes[overlay_volume_name(SYSTEM)] = _Volume(pool, "ov", _overlay_xml(107, 107))

    path = _volumes().create(_Conn(pool), DOMAIN, device_id, 4)

    name = scratch_volume_name(SYSTEM, device_id)
    assert path == f"/pool/{name}"
    xml = ET.fromstring(pool.created_xml[0])
    assert xml.findtext("capacity") == str(4 * 1024**3)
    assert _el(xml, "target/format").get("type") == "qcow2"  # type: ignore[union-attr]
    assert xml.findtext("target/permissions/owner") == "107"
    assert xml.find("backingStore") is None


def test_create_without_an_overlay_keeps_pool_defaults_and_replaces_a_stale_volume() -> None:
    pool, device_id = _Pool(), str(uuid4())
    stale = scratch_volume_name(SYSTEM, device_id)
    pool.volumes[stale] = _Volume(pool, stale)

    _volumes().create(_Conn(pool), DOMAIN, device_id, 1)

    assert ET.fromstring(pool.created_xml[0]).find("target/permissions") is None
    assert list(pool.volumes) == [stale]


def test_remove_all_deletes_only_this_systems_scratch_volumes() -> None:
    pool, mine, theirs = _Pool(), str(uuid4()), str(uuid4())
    other = UUID("33333333-3333-3333-3333-333333333333")
    keep = [overlay_volume_name(SYSTEM), scratch_volume_name(other, theirs)]
    for name in [*keep, scratch_volume_name(SYSTEM, mine)]:
        pool.volumes[name] = _Volume(pool, name)

    _volumes().remove_all(_Conn(pool), DOMAIN)
    _volumes().remove(_Conn(pool), DOMAIN, mine)  # absent: no error

    assert sorted(pool.volumes) == sorted(keep)


def test_missing_pool_is_a_configuration_error() -> None:
    with pytest.raises(CategorizedError) as err:
        _volumes().remove_all(_Conn(None), DOMAIN)

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_non_system_domain_is_refused() -> None:
    with pytest.raises(CategorizedError) as err:
        _volumes().remove_all(_Conn(_Pool()), "someone-elses-guest")

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_volume_xml_omits_permissions_without_an_identity() -> None:
    xml: Any = ET.fromstring(render_scratch_volume_xml("v", size_gb=1, identity=None))
    assert xml.find("target/permissions") is None


class _AbsentDomain:
    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        return "<domain><devices/></domain>"


class _CountingConn(_Conn):
    """A host connection whose domain has no kdive device; counts storage-pool lookups."""

    def __init__(self) -> None:
        super().__init__(_Pool())
        self.pool_lookups = 0

    def lookupByName(self, name: str) -> _AbsentDomain:  # noqa: N802
        return _AbsentDomain()

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        self.pool_lookups += 1
        return super().storagePoolLookupByName(name)


def _absent_port(conn: _CountingConn) -> LibvirtDeviceHotplug:
    @contextmanager
    def connection() -> Generator[_CountingConn]:
        yield conn

    return LibvirtDeviceHotplug(
        connection=connection,
        volumes=_volumes(),
        nic_source=lambda _network: ("user", None),
        kinds=frozenset(HotplugDeviceKind),
    )


@pytest.mark.parametrize(
    ("kind", "lookups"), [(HotplugDeviceKind.NIC, 0), (HotplugDeviceKind.DISK, 2)]
)
def test_absent_device_touches_the_storage_pool_only_for_a_disk(
    kind: HotplugDeviceKind, lookups: int
) -> None:
    conn, device_id = _CountingConn(), str(uuid4())
    port = _absent_port(conn)

    assert port.unplug(DOMAIN, device_id, None, kind=kind) is True
    assert port.is_attached(DOMAIN, device_id, None, kind=kind) is False

    assert conn.pool_lookups == lookups
