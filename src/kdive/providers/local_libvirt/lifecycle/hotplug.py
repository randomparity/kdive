"""Local-libvirt device hot-plug (ADR-0823): NICs and qcow2 scratch disks on a live domain.

The domain logic is :class:`~kdive.providers.shared.libvirt_hotplug.LibvirtDeviceHotplug`; this
module supplies the local seams: ``libvirt.open`` on ``KDIVE_LIBVIRT_URI``, scratch volumes as
qcow2 files beside the System rootfs overlay, and a user-mode NIC when no network is named (the
unprivileged session daemon has no libvirt network, ADR-0804). A named network is attached as a
libvirt network, so an unknown one surfaces as ``unknown_network``.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Final

import libvirt

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.storage import ROOTFS_DIR, create_scratch_qcow2
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.ports.lifecycle import HotplugDeviceKind
from kdive.providers.shared.libvirt_hotplug import (
    HotplugConnection,
    LibvirtDeviceHotplug,
    NicSource,
    libvirt_failure,
)

_log = logging.getLogger(__name__)

ADVERTISED_KINDS: Final = frozenset({HotplugDeviceKind.NIC, HotplugDeviceKind.DISK})


def scratch_disk_prefix(domain_name: str) -> str:
    """The file-name prefix of every scratch disk of ``domain_name``."""
    return f"{domain_name}-dev-"


class LocalDeviceVolumes:
    """Scratch disks as ``<rootfs root>/<domain>-dev-<device_id>.qcow2``."""

    def __init__(self, root: str = ROOTFS_DIR) -> None:
        self._root = Path(root)

    def _path(self, domain_name: str, device_id: str) -> Path:
        return self._root / f"{scratch_disk_prefix(domain_name)}{device_id}.qcow2"

    def create(self, conn: Any, domain_name: str, device_id: str, size_gb: int, /) -> str:
        path = self._path(domain_name, device_id)
        create_scratch_qcow2(str(path), size_gb)
        return str(path)

    def remove(self, conn: Any, domain_name: str, device_id: str, /) -> None:
        self._unlink(self._path(domain_name, device_id))

    def remove_all(self, conn: Any, domain_name: str, /) -> None:
        for path in self._root.glob(f"{scratch_disk_prefix(domain_name)}*.qcow2"):
            self._unlink(path)

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise CategorizedError(
                "failed to remove a scratch disk",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={
                    "op": "remove_scratch_disk",
                    "disk": path.name,
                    "error": type(exc).__name__,
                },
            ) from exc


def _nic_source(network: str | None) -> NicSource:
    return ("user", None) if network is None else ("network", network)


def _open(host_uri: str) -> AbstractContextManager[HotplugConnection]:
    @contextmanager
    def connection() -> Generator[HotplugConnection]:
        try:
            conn = libvirt.open(host_uri)
        except libvirt.libvirtError as exc:
            raise libvirt_failure(exc, "connecting to libvirt for", "hotplug") from exc
        try:
            yield conn
        finally:
            try:
                conn.close()
            except libvirt.libvirtError:
                _log.warning("libvirt connection close failed; continuing", exc_info=True)

    return connection()


def build_local_hotplug(
    *,
    host_uri: str | None = None,
    volumes: LocalDeviceVolumes | None = None,
) -> LibvirtDeviceHotplug:
    """Build the local hot-plug port; ``host_uri`` defaults to ``KDIVE_LIBVIRT_URI``."""
    uri = host_uri if host_uri is not None else config.require(LIBVIRT_URI)
    return LibvirtDeviceHotplug(
        connection=lambda: _open(uri),
        volumes=volumes if volumes is not None else LocalDeviceVolumes(),
        nic_source=_nic_source,
        kinds=ADVERTISED_KINDS,
    )
