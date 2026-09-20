"""Environment and invocation cleanup for the remote supplied ROOTFS proof (#1516)."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.connection.transport import RemoteLibvirtConnections
from kdive.providers.remote_libvirt.connection.uri_validation import validate_remote_transport
from kdive.providers.remote_libvirt.lifecycle.provisioning import RemoteLibvirtProvisioning
from kdive.providers.remote_libvirt.lifecycle.xml import (
    overlay_volume_name,
    supplied_base_volume_name,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.live_vm.remote_external_boot_support import attempt_all_cleanup

ROOTFS_ENV = "KDIVE_LIVE_VM_REMOTE_ROOTFS"


@dataclass(frozen=True)
class RootfsContract:
    source: Path
    uri: str
    base_image: str
    gdb_addr: str


def require_remote_rootfs() -> RootfsContract:
    source = os.environ.get(ROOTFS_ENV)
    if not source:
        pytest.skip(f"{ROOTFS_ENV} unset; supplied ROOTFS carrier is not configured")
    required = (
        "KDIVE_LIVE_VM_REMOTE_URI",
        "KDIVE_LIVE_VM_REMOTE_BASE_IMAGE",
        "KDIVE_LIVE_VM_REMOTE_GDB_ADDR",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.fail("supplied ROOTFS carrier requires " + ", ".join(missing))
    uri, base_image, gdb_addr = (os.environ[name] for name in required)
    try:
        validate_remote_transport(uri)
        parsed = urlsplit(uri)
        if not parsed.hostname or parsed.path != "/system":
            raise ValueError("URI must name a remote host and /system")
        ip_address(gdb_addr)
        path = Path(source)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("ROOTFS must be an absolute readable regular file")
        with path.open("rb") as handle:
            if handle.read(4) != b"QFI\xfb":
                raise ValueError("ROOTFS must be qcow2")
        if not base_image.strip():
            raise ValueError("comparison base-image volume must be nonempty")
    except (CategorizedError, ValueError, OSError) as exc:
        pytest.fail(f"invalid supplied ROOTFS carrier configuration: {exc}")
    return RootfsContract(path.resolve(), uri, base_image, gdb_addr)


class _Connections:
    """Use real libvirt connections with the operator's existing TLS credential setup."""

    def __init__(self, contract: RootfsContract) -> None:
        self._config = RemoteLibvirtConfig(
            uri=contract.uri,
            cert_refs=TlsCertRefs("unused", "unused", "unused"),
            concurrent_allocation_cap=1,
            gdb_addr=contract.gdb_addr,
        )

    def config(self) -> RemoteLibvirtConfig:
        return self._config

    @contextmanager
    def connection(self, config: RemoteLibvirtConfig) -> Iterator[libvirt.virConnect]:
        conn = libvirt.open(config.uri)
        try:
            yield conn
        finally:
            conn.close()


@dataclass
class RootfsCase:
    contract: RootfsContract
    system_id: UUID
    provisioner: RemoteLibvirtProvisioning

    @property
    def domain(self) -> str:
        return f"kdive-{self.system_id}"

    @property
    def volumes(self) -> tuple[str, str]:
        return overlay_volume_name(self.system_id), supplied_base_volume_name(self.system_id)

    def profile(self, *, supplied: bool, source: Path | None = None) -> ProvisioningProfile:
        section = (
            {"base_image_source": {"kind": "local", "path": str(source or self.contract.source)}}
            if supplied
            else {"base_image_volume": self.contract.base_image}
        )
        return ProvisioningProfile.parse(
            {
                "schema_version": 1,
                "arch": "x86_64",
                "vcpu": 2,
                "memory_mb": 2048,
                "disk_gb": 20,
                "boot_method": "disk-image",
                "provider": {"remote-libvirt": section},
            }
        )

    def assert_absent(self) -> None:
        connections = _Connections(self.contract)
        with connections.connection(connections.config()) as conn:
            assert_domain_absent(conn, self.domain)
            pool = conn.storagePoolLookupByName("default")
            for name in self.volumes:
                assert_volume_absent(pool, name)

    def assert_booted_backing(self, *, supplied: bool) -> None:
        connections = _Connections(self.contract)
        with connections.connection(connections.config()) as conn:
            domain = conn.lookupByName(self.domain)
            assert domain.isActive() == 1
            pool = conn.storagePoolLookupByName("default")
            base_name = self.volumes[1] if supplied else self.contract.base_image
            base = pool.storageVolLookupByName(base_name)
            overlay = pool.storageVolLookupByName(self.volumes[0])
            assert ET.fromstring(overlay.XMLDesc(0)).findtext("./backingStore/path") == base.path()
            disk = ET.fromstring(domain.XMLDesc(0)).find("./devices/disk/source")
            assert disk is not None
            assert disk.get("file") == overlay.path()


def assert_volume_absent(pool: libvirt.virStoragePool, name: str) -> None:
    with pytest.raises(libvirt.libvirtError) as caught:
        pool.storageVolLookupByName(name)
    assert caught.value.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL


def assert_domain_absent(conn: libvirt.virConnect, name: str) -> None:
    with pytest.raises(libvirt.libvirtError) as caught:
        conn.lookupByName(name)
    assert caught.value.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN


def stable_volume_xml(xml: str) -> str:
    """Exclude libvirt's dynamic ownership/access bookkeeping, not image identity."""
    root = ET.fromstring(xml)
    for parent_path, name in (
        (".", "allocation"),
        ("./target/permissions", "owner"),
        ("./target/permissions", "group"),
        ("./target/timestamps", "atime"),
        ("./target/timestamps", "ctime"),
    ):
        parent = root.find(parent_path)
        if parent is not None and (element := parent.find(name)) is not None:
            parent.remove(element)
    return ET.canonicalize(ET.tostring(root, encoding="unicode"), strip_text=True)


@contextmanager
def rootfs_case(
    contract: RootfsContract, *, allowed_roots: tuple[Path, ...] | None = None
) -> Iterator[RootfsCase]:
    connections = _Connections(contract)
    provisioner = RemoteLibvirtProvisioning(
        secret_registry=SecretRegistry(),
        connections=cast(RemoteLibvirtConnections, connections),
        allowed_roots=allowed_roots or (contract.source.parent,),
    )
    case = RootfsCase(contract, uuid4(), provisioner)
    case.assert_absent()
    assert contract.base_image not in case.volumes
    with connections.connection(connections.config()) as conn:
        pool = conn.storagePoolLookupByName("default")
        operator = pool.storageVolLookupByName(contract.base_image)
        operator_before = stable_volume_xml(operator.XMLDesc(0))
    primary: Exception | None = None
    try:
        yield case
    except (Exception, pytest.fail.Exception) as exc:
        primary = exc if isinstance(exc, Exception) else AssertionError(str(exc))
        if primary is not exc:
            primary.__cause__ = exc
    finally:

        def cleanup() -> None:
            with connections.connection(connections.config()) as conn:

                def remove_domain() -> None:
                    try:
                        domain = conn.lookupByName(case.domain)
                    except libvirt.libvirtError as exc:
                        if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                            raise
                        return
                    if domain.isActive():
                        domain.destroy()
                    domain.undefine()

                def remove_volume(name: str) -> None:
                    pool = conn.storagePoolLookupByName("default")
                    try:
                        volume = pool.storageVolLookupByName(name)
                    except libvirt.libvirtError as exc:
                        if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
                            raise
                        return
                    volume.delete(0)

                def preserve_operator() -> None:
                    pool = conn.storagePoolLookupByName("default")
                    current = pool.storageVolLookupByName(contract.base_image).XMLDesc(0)
                    assert stable_volume_xml(current) == operator_before

                attempt_all_cleanup(
                    [
                        ("scratch domain", remove_domain),
                        ("scratch overlay", lambda: remove_volume(case.volumes[0])),
                        ("scratch base", lambda: remove_volume(case.volumes[1])),
                        ("scratch absence", case.assert_absent),
                        ("operator base", preserve_operator),
                    ],
                )

        attempt_all_cleanup([("remote cleanup", cleanup)], primary=primary)
