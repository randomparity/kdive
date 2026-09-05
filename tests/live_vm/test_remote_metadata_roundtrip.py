"""Native remote-libvirt metadata round-trip proof (ADR-0598)."""

from __future__ import annotations

import copy
import sys
import xml.etree.ElementTree as ET
from contextlib import closing
from typing import Protocol, cast
from uuid import UUID, uuid4

import libvirt
import pytest
from defusedxml.ElementTree import fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.remote_libvirt.lifecycle.external_boot import require_disk_grub_source
from kdive.providers.remote_libvirt.lifecycle.storage import OverlayPool, ensure_overlay
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name, render_domain_xml
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS
from tests.live_vm import require_live_vm_remote

_POOL = "default"


class _Domain(Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802
    def undefine(self) -> object: ...


class _Volume(Protocol):
    def delete(self, flags: int = 0) -> object: ...


class _Pool(Protocol):
    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802


def _profile(base_image: str) -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "provider": {
                "remote-libvirt": {"base_image_volume": base_image, "crashkernel": "256M"}
            },
        }
    )


def _assert_rejected(xml: str, system_id: UUID, overlay_path: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        require_disk_grub_source(xml, system_id=system_id, pool=_POOL, overlay_path=overlay_path)
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["rule"] == "boot-disk"


def _required(root: ET.Element, path: str) -> ET.Element:
    element = root.find(path)
    assert element is not None
    return element


def _cleanup(domain: _Domain | None, pool: _Pool, overlay_name: str) -> None:
    failures: list[str] = []
    if domain is not None:
        try:
            domain.undefine()
        except libvirt.libvirtError:
            failures.append("domain undefine")
    try:
        pool.storageVolLookupByName(overlay_name).delete(0)
    except libvirt.libvirtError:
        failures.append("overlay delete")
    if failures:
        raise AssertionError("remote metadata proof cleanup failed: " + ", ".join(failures))


def test_cleanup_attempts_overlay_delete_after_undefine_failure() -> None:
    class Domain:
        def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
            del flags
            return "<domain/>"

        def undefine(self) -> None:
            raise libvirt.libvirtError("failed")

    class Volume:
        deleted = False

        def delete(self, flags: int = 0) -> None:
            del flags
            self.deleted = True

    class Pool:
        volume = Volume()

        def storageVolLookupByName(self, name: str) -> Volume:  # noqa: N802
            assert name == "owned-overlay"
            return self.volume

    pool = Pool()
    with pytest.raises(AssertionError, match="domain undefine"):
        _cleanup(Domain(), pool, "owned-overlay")
    assert pool.volume.deleted


@pytest.mark.live_vm
@pytest.mark.live_vm_remote
def test_remote_metadata_round_trips_and_binds_storage_identity() -> None:
    contract = require_live_vm_remote()
    system_id = uuid4()
    overlay_name = overlay_volume_name(system_id)
    domain: _Domain | None = None
    with closing(libvirt.open(contract.libvirt_uri)) as conn:
        pool = conn.storagePoolLookupByName(_POOL)
        overlay = ensure_overlay(cast("OverlayPool", pool), contract.base_image, system_id)
        try:
            domain = cast(
                "_Domain",
                conn.defineXML(
                    render_domain_xml(
                        system_id,
                        _profile(contract.base_image),
                        pool=_POOL,
                        volume=overlay_name,
                        overlay_path=overlay.path,
                        backing_path=overlay.backing_path,
                        gdb_addr="127.0.0.1",
                        gdb_port=55998,
                    )
                ),
            )
            inactive = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            root = fromstring(inactive)
            ownership = root.find(f"./metadata/{{{KDIVE_METADATA_NS}}}domain")
            assert ownership is not None
            assert ownership.findtext(f"./{{{KDIVE_METADATA_NS}}}system") == str(system_id)
            storage = ownership.find(f"./{{{KDIVE_METADATA_NS}}}storage")
            assert storage is not None
            assert (storage.get("pool"), storage.get("volume")) == (_POOL, overlay_name)
            require_disk_grub_source(
                inactive, system_id=system_id, pool=_POOL, overlay_path=overlay.path
            )

            wrong_pool = copy.deepcopy(root)
            storage_path = (
                f"./metadata/{{{KDIVE_METADATA_NS}}}domain/{{{KDIVE_METADATA_NS}}}storage"
            )
            _required(wrong_pool, storage_path).set("pool", "other")
            _assert_rejected(ET.tostring(wrong_pool, encoding="unicode"), system_id, overlay.path)

            wrong_volume = copy.deepcopy(root)
            _required(wrong_volume, storage_path).set("volume", "other.qcow2")
            _assert_rejected(ET.tostring(wrong_volume, encoding="unicode"), system_id, overlay.path)

            wrong_path = copy.deepcopy(root)
            _required(wrong_path, "./devices/disk/source").set("file", "/unrelated/other.qcow2")
            _assert_rejected(ET.tostring(wrong_path, encoding="unicode"), system_id, overlay.path)
        finally:
            primary = sys.exception()
            try:
                _cleanup(domain, cast("_Pool", pool), overlay_name)
            except AssertionError as cleanup_error:
                if isinstance(primary, Exception):
                    raise ExceptionGroup(
                        "remote metadata proof and cleanup both failed", [primary, cleanup_error]
                    ) from None
                raise
