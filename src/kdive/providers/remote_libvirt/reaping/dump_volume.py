"""Remote-libvirt host_dump orphaned-volume reaper (#301, ADR-0094).

The reconciler's stateless orphan-volume sweep consumes this provider port (the
``DumpVolumeReaper`` contract) to reap host_dump volumes orphaned by a non-graceful
worker/host crash that bypassed the capture's ``finally`` cleanup. It lists the storage
pool's dump volumes (matched by the deterministic ``kdive-host-dump-<system_id>.kdump``
name) with each volume's store mtime — read from the volume XML's ``<timestamps>/<mtime>``,
which libvirt populates for filesystem/dir-backed pools — and deletes one by name. The
reconciler owns both live-holder guards (no active capture job, mtime older than the grace
window); this port is the narrow libvirt I/O seam. The blocking libvirt calls run only under
the ``live_vm`` gate; orchestration + name/mtime parsing are unit-tested with fakes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import DumpVolume
from kdive.providers.remote_libvirt.connection.transport import (
    RemoteLibvirtConnections,
)
from kdive.providers.remote_libvirt.reaping.connections import (
    find_over_fleet,
    map_over_fleet,
    open_libvirt_reaper,
    remote_libvirt_reaper_connections,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)

# The deterministic dump-volume name carries the owning System's UUID (ADR-0094).
_DUMP_VOLUME_RE = re.compile(
    r"^kdive-host-dump-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.kdump$"
)


class _Volume(Protocol):
    def name(self) -> str: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - libvirt binding name
    def delete(self, flags: int = 0) -> int: ...


class _Pool(Protocol):
    def listAllVolumes(self, flags: int = 0) -> list[_Volume]: ...  # noqa: N802 - binding name
    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802 - binding name
    def refresh(self, flags: int = 0) -> int: ...


class _ReaperConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


type OpenDumpReaperConnection = Callable[[str], _ReaperConn]


def system_id_from_dump_volume_name(name: str) -> UUID | None:
    """The owning System UUID encoded in a dump-volume name, or ``None`` if it does not match."""
    match = _DUMP_VOLUME_RE.match(name)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:  # pragma: no cover - the regex already constrains the shape
        return None


def volume_mtime_epoch_s(volume_xml: str) -> float:
    """The volume's mtime (epoch seconds) from its XML ``<target>/<timestamps>/<mtime>``.

    libvirt populates ``<timestamps>`` for filesystem/dir-backed pools as ``sec.nsec``. A
    document without it (or a malformed one) yields ``0.0`` — epoch, which the reconciler's
    age check treats as old enough to consider for reaping, falling back to the active-capture
    guard so a missing timestamp never *protects* a true orphan from cleanup.
    """
    try:
        root = _safe_fromstring(volume_xml)
    except Exception:  # noqa: BLE001 - host-emitted XML; a parse failure reads as no timestamp
        return 0.0
    mtime = root.findtext("./target/timestamps/mtime")
    if mtime is None:
        return 0.0
    try:
        return float(mtime)
    except ValueError:
        return 0.0


class RemoteLibvirtDumpVolumeReaper:
    """List + delete host_dump volumes in the operator's storage pool (the reconciler seam)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        connections: RemoteLibvirtConnections[_ReaperConn] | None = None,
    ) -> None:
        self._connections = connections or remote_libvirt_reaper_connections(
            secret_registry=secret_registry,
            open_connection=open_libvirt_reaper,
        )

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtDumpVolumeReaper:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    async def list_dump_volumes(self) -> list[DumpVolume]:
        """List the storage pool's host_dump volumes with their store mtime (offloaded)."""
        return await asyncio.to_thread(self._list_blocking)

    async def delete_dump_volume(self, name: str) -> None:
        """Delete one dump volume by name; a volume already gone is not an error (offloaded)."""
        await asyncio.to_thread(self._delete_blocking, name)

    def _list_blocking(self) -> list[DumpVolume]:  # pragma: no cover - live_vm
        per_host = map_over_fleet(
            self._connections,
            lambda conn, config: self._list_host(conn, config.storage_pool),
            operation="dump-volume list",
        )
        return [vol for host in per_host for vol in host]

    @staticmethod
    def _list_host(conn: _ReaperConn, storage_pool: str) -> list[DumpVolume]:  # pragma: no cover
        pool = conn.storagePoolLookupByName(storage_pool)
        pool.refresh(0)
        volumes: list[DumpVolume] = []
        for volume in pool.listAllVolumes(0):
            name = volume.name()
            system_id = system_id_from_dump_volume_name(name)
            if system_id is None and not name.startswith("kdive-host-dump-"):
                continue
            volumes.append(
                DumpVolume(
                    name=name,
                    system_id=system_id,
                    mtime_epoch_s=volume_mtime_epoch_s(volume.XMLDesc(0)),
                )
            )
        return volumes

    def _delete_blocking(self, name: str) -> None:  # pragma: no cover - live_vm
        # A dump-volume name encodes the owning System but not its host, so the reconciler calls
        # delete-by-name with no host. find_over_fleet tries each declared host (isolating an
        # unreachable one) and stops at the one that has the volume; an already-gone or
        # not-on-this-host volume is benign — never an error.
        find_over_fleet(
            self._connections,
            lambda conn, config: self._delete_on_host(conn, config.storage_pool, name),
            operation="dump-volume delete",
        )

    @staticmethod
    def _delete_on_host(  # pragma: no cover - live_vm
        conn: _ReaperConn, storage_pool: str, name: str
    ) -> bool:
        pool = conn.storagePoolLookupByName(storage_pool)
        try:
            volume = pool.storageVolLookupByName(name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return False  # not on this host (or already gone) — try the next
            raise _infra("looking up host_dump volume", volume=name) from exc
        try:
            volume.delete(0)
        except libvirt.libvirtError as exc:
            raise _infra("deleting host_dump volume", volume=name) from exc
        _log.info("reconciler: deleted orphaned host_dump volume %s", name)
        return True


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )


__all__ = [
    "RemoteLibvirtDumpVolumeReaper",
    "OpenDumpReaperConnection",
    "system_id_from_dump_volume_name",
    "volume_mtime_epoch_s",
]
