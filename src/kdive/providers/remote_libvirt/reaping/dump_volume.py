"""Remote-libvirt host_dump orphaned-volume reaper (#301, ADR-0094).

The reconciler's stateless orphan-volume sweep consumes this provider port (the
``DumpVolumeReaper`` contract) to reap host_dump volumes orphaned by a non-graceful
worker/host crash that bypassed the capture's ``finally`` cleanup. It lists the storage
pool's dump volumes (matched by the deterministic ``kdive-host-dump-<system_id>.kdump``
name) with each volume's store mtime — read from the volume XML's ``<timestamps>/<mtime>``,
which libvirt populates for filesystem/dir-backed pools — and deletes one by name **and sampled
mtime**: the deterministic name is reused by every capture of that System, so the delete re-reads
the volume it looked up and declines a volume that is no longer the one the reconciler classified
(ADR-0562). The reconciler owns the live-holder guards (no live capture lease, no active capture
job, mtime older than the grace window) and holds the System's advisory lock across both its final
classification and this delete; this port is the narrow libvirt I/O seam. The blocking libvirt calls
run only under the ``live_vm`` gate; orchestration + name/mtime parsing are unit-tested with fakes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import DumpVolume
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
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


class _HostOutcome(StrEnum):
    """What one host did with a delete-by-name-and-identity request.

    Three states rather than the fan-out's two, because "this host handled the name" and "the volume
    is gone" are different facts once the delete can decline: a decline stops the fan-out (the name
    belongs to this host) while leaving the volume in place (ADR-0562).
    """

    #: No volume of that name here — the fan-out should try the next host.
    NOT_HERE = "not_here"
    #: A volume of that name is here, but it is not the one the reconciler classified.
    DECLINED = "declined"
    DELETED = "deleted"


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

    async def delete_dump_volume(self, name: str, *, expected_mtime_epoch_s: float) -> bool:
        """Delete the dump volume the reconciler sampled; a volume already gone is not an error.

        ``expected_mtime_epoch_s`` is the mtime :meth:`list_dump_volumes` reported for this volume.
        A volume whose mtime has changed since then is not the one the reconciler classified — the
        deterministic name is reused by every capture of that System — and is left alone (ADR-0562).
        Offloaded, like every libvirt call here.

        Returns:
            Whether the name is gone: ``True`` when a host deleted it or no host had it, ``False``
            when a host holds a volume of that name whose identity does not match the sample.
        """
        return await asyncio.to_thread(self._delete_blocking, name, expected_mtime_epoch_s)

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

    def _delete_blocking(self, name: str, expected_mtime_epoch_s: float) -> bool:
        # A dump-volume name encodes the owning System but not its host, so the reconciler calls
        # delete-by-name with no host. find_over_fleet tries each declared host (isolating an
        # unreachable one) and stops at the one that has the volume; an already-gone or
        # not-on-this-host volume is benign — never an error.
        #
        # find_over_fleet's own bool means "this host handled the name", which a decline also is —
        # so the decline is carried out separately. Reporting a decline as "not mine" would send the
        # fan-out looking for another host's copy of the same deterministic name, and delete it.
        declined = False

        def delete_here(conn: _ReaperConn, config: RemoteLibvirtConfig) -> bool:
            nonlocal declined
            outcome = self._delete_on_host(conn, config.storage_pool, name, expected_mtime_epoch_s)
            declined = outcome is _HostOutcome.DECLINED
            return outcome is not _HostOutcome.NOT_HERE

        find_over_fleet(self._connections, delete_here, operation="dump-volume delete")
        return not declined

    @staticmethod
    def _delete_on_host(
        conn: _ReaperConn, storage_pool: str, name: str, expected_mtime_epoch_s: float
    ) -> _HostOutcome:
        """Delete this host's copy of ``name`` if it is still the volume the reconciler sampled.

        The mtime re-read sits between the lookup and the delete, on the volume object the lookup
        returned, so nothing can substitute a volume in between (ADR-0562). A mismatch means a
        capture recreated the deterministic name after the reconciler classified the old one: this
        host has handled the name, so the fan-out stops, and nothing is deleted.

        A pool whose volume XML carries no ``<timestamps>`` reports ``0.0`` on both reads, so this
        comparison cannot distinguish a recreated volume there. Such a pool has already lost
        ADR-0094's mtime grace for the same reason; the lease and the reconciler's System lock are
        its guards, and this is the backstop for the writer they cannot see.
        """
        pool = conn.storagePoolLookupByName(storage_pool)
        try:
            volume = pool.storageVolLookupByName(name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return _HostOutcome.NOT_HERE  # not on this host (or already gone)
            raise _infra("looking up host_dump volume", volume=name) from exc
        try:
            observed_mtime = volume_mtime_epoch_s(volume.XMLDesc(0))
        except libvirt.libvirtError as exc:
            raise _infra("re-reading host_dump volume identity", volume=name) from exc
        if observed_mtime != expected_mtime_epoch_s:
            _log.info(
                "reconciler: host_dump volume %s changed since it was classified "
                "(sampled mtime %s, now %s); leaving it to the next pass",
                name,
                expected_mtime_epoch_s,
                observed_mtime,
            )
            return _HostOutcome.DECLINED
        try:
            volume.delete(0)
        except libvirt.libvirtError as exc:
            raise _infra("deleting host_dump volume", volume=name) from exc
        _log.info("reconciler: deleted orphaned host_dump volume %s", name)
        return _HostOutcome.DELETED


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
