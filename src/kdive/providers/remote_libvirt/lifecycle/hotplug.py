"""Remote-libvirt device hot-plug (ADR-0823): NICs and qcow2 scratch disks over qemu+tls.

The domain logic is :class:`~kdive.providers.shared.libvirt_hotplug.LibvirtDeviceHotplug`; this
module supplies the remote seams: the mutual-TLS connection of ``remote_connection`` (ADR-0077),
scratch disks as qcow2 volumes in the host's storage pool (ADR-0080), and a libvirt network NIC on
the configured network. The per-Resource config factory resolves the allocated host, exactly as
for the snapshotter. No shared layer with ``local_libvirt`` (ADR-0076).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.lifecycle import HotplugDeviceKind
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, unbound_remote_config
from kdive.providers.remote_libvirt.connection.transport import (
    open_libvirt_protocol,
    remote_connection,
)
from kdive.providers.remote_libvirt.lifecycle.xml import (
    overlay_volume_name,
    volume_target_identity,
)
from kdive.providers.shared.libvirt_hotplug import (
    HotplugConnection,
    LibvirtDeviceHotplug,
    NicSource,
)
from kdive.providers.shared.runtime_paths import system_id_from_domain_name
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)

ADVERTISED_KINDS: Final = frozenset({HotplugDeviceKind.NIC, HotplugDeviceKind.DISK})
_BYTES_PER_GB: Final = 1024**3


class _Volume(Protocol):
    def delete(self, flags: int = 0) -> int: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - libvirt binding name


class _Pool(Protocol):
    def refresh(self, flags: int = 0) -> int: ...
    def listVolumes(self) -> list[str]: ...  # noqa: N802 - libvirt binding name
    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802
    def createXML(self, xml: str, flags: int = 0) -> Any: ...  # noqa: N802


def scratch_volume_prefix(system_id: UUID) -> str:
    """The pool-volume name prefix of every scratch disk of one System."""
    return f"kdive-{system_id}-dev-"


def scratch_volume_name(system_id: UUID, device_id: str) -> str:
    """The pool volume name of one scratch disk."""
    return f"{scratch_volume_prefix(system_id)}{device_id}.qcow2"


def render_scratch_volume_xml(name: str, *, size_gb: int, identity: tuple[int, int] | None) -> str:
    """Volume XML of an empty qcow2; ``identity`` is the owner/group the System overlay carries."""
    volume = ET.Element("volume")
    ET.SubElement(volume, "name").text = name
    ET.SubElement(volume, "capacity").text = str(size_gb * _BYTES_PER_GB)
    target = ET.SubElement(volume, "target")
    ET.SubElement(target, "format", type="qcow2")
    if identity is not None:
        permissions = ET.SubElement(target, "permissions")
        ET.SubElement(permissions, "owner").text = str(identity[0])
        ET.SubElement(permissions, "group").text = str(identity[1])
        ET.SubElement(permissions, "mode").text = "0600"
    return ET.tostring(volume, encoding="unicode")


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )


class RemoteDeviceVolumes:
    """Scratch disks as pool volumes named ``kdive-<system>-dev-<device_id>.qcow2``."""

    def __init__(self, config_factory: Callable[[], RemoteLibvirtConfig]) -> None:
        self._config_factory = config_factory

    def create(self, conn: Any, domain_name: str, device_id: str, size_gb: int, /) -> str:
        system_id = self._system_id(domain_name)
        pool = self._pool(conn)
        name = scratch_volume_name(system_id, device_id)
        self._delete_named(pool, name)
        identity = self._overlay_identity(pool, system_id)
        try:
            volume = pool.createXML(
                render_scratch_volume_xml(name, size_gb=size_gb, identity=identity)
            )
            return str(volume.path())
        except libvirt.libvirtError as exc:
            raise _infra("creating the scratch volume", volume=name) from exc

    def remove(self, conn: Any, domain_name: str, device_id: str, /) -> None:
        name = scratch_volume_name(self._system_id(domain_name), device_id)
        self._delete_named(self._pool(conn), name)

    def remove_all(self, conn: Any, domain_name: str, /) -> None:
        prefix = scratch_volume_prefix(self._system_id(domain_name))
        pool = self._pool(conn)
        try:
            names = [name for name in pool.listVolumes() if name.startswith(prefix)]
        except libvirt.libvirtError as exc:
            raise _infra("listing storage volumes") from exc
        for name in names:
            self._delete_named(pool, name)

    @staticmethod
    def _system_id(domain_name: str) -> UUID:
        system_id = system_id_from_domain_name(domain_name)
        if system_id is None:
            raise CategorizedError(
                f"domain {domain_name} is not a kdive System domain",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"domain": domain_name},
            )
        return system_id

    def _pool(self, conn: Any) -> _Pool:
        pool_name = self._config_factory().storage_pool
        try:
            pool: _Pool = conn.storagePoolLookupByName(pool_name)
            pool.refresh()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
                raise CategorizedError(
                    f"storage pool {pool_name!r} does not exist on the remote host",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"pool": pool_name},
                ) from exc
            raise _infra("looking up storage pool", pool=pool_name) from exc
        return pool

    @staticmethod
    def _delete_named(pool: _Pool, name: str) -> None:
        try:
            pool.storageVolLookupByName(name).delete()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return
            raise _infra("deleting the scratch volume", volume=name) from exc

    @staticmethod
    def _overlay_identity(pool: _Pool, system_id: UUID) -> tuple[int, int] | None:
        try:
            overlay_xml = pool.storageVolLookupByName(overlay_volume_name(system_id)).XMLDesc()
            return volume_target_identity(overlay_xml)
        except libvirt.libvirtError, ValueError:
            _log.info("no overlay identity for %s; scratch volume keeps pool defaults", system_id)
            return None


class RemoteLibvirtDeviceHotplug(LibvirtDeviceHotplug):
    """The remote-libvirt hot-plug port."""

    @classmethod
    def from_env(
        cls,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
        open_connection: Callable[[str], Any] = open_libvirt_protocol,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
    ) -> RemoteLibvirtDeviceHotplug:
        """Build from the shared worker env; opens no connection here."""
        backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )

        def connection() -> AbstractContextManager[HotplugConnection]:
            return remote_connection(
                config_factory(),
                backend_factory(),
                open_connection=open_connection,
                pki_base_dir=pki_base_dir,
            )

        def nic_source(network: str | None) -> NicSource:
            return ("network", network if network is not None else config_factory().network)

        return cls(
            connection=connection,
            volumes=RemoteDeviceVolumes(config_factory),
            nic_source=nic_source,
            kinds=ADVERTISED_KINDS,
        )


__all__ = ["ADVERTISED_KINDS", "RemoteDeviceVolumes", "RemoteLibvirtDeviceHotplug"]
