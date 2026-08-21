"""Remote-libvirt orphaned-traffic-capture reaper (ADR-0556, #1947).

The concrete ``CaptureReaper`` for the ``remote-libvirt`` kind. Over **one** connection bound
to the captured Resource's own declared ``[[remote_libvirt]]`` instance (ADR-0187, via
:func:`remote_config_for_resource`), it detaches the job's QEMU ``filter-dump`` object and then
deletes the deterministic pcap volume.

The ordering is the control, not a style choice: QEMU holds the destination's fd, so deleting
the volume first reclaims no space until the domain stops, leaves the filter appending into an
unlinked inode, and hides the volume from ``storageVolLookupByName`` — a bounded visible leak
becomes an unbounded invisible one (ADR-0556).

Host binding is per-Resource, never the fleet bundle in :mod:`.connections`: the ownership
chain names the Resource that owned the capture, so fanning out would cost one connect per
declared host per row and could touch a host that never held it. The opener is still that
seam's :func:`open_libvirt_reaper`, so the ADR-0565 reachability gate bounds an unreachable
host instead of libvirt's ~130 s kernel TCP connect timeout — the retry-budget property the
:class:`~kdive.providers.infra.reaping.CaptureReaper` port requires.

Like every reaper under ADR-0556 this is idempotent: a crash between a successful provider
call and the sweep's completion marker repeats an already-effective call, so an already-missing
filter, domain, or volume is tolerated. A libvirt error that is *not* an absence surfaces as a
``CategorizedError`` — a degraded host must defer the row, not record a reclaim that did not
happen.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import OrphanedCapture
from kdive.providers.ports.traffic import capture_qom_id, pcap_volume_name
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_for_resource
from kdive.providers.remote_libvirt.connection.transport import remote_connection
from kdive.providers.remote_libvirt.reaping.connections import open_libvirt_reaper
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)


class _ReaperDomain(Protocol):
    def qemuMonitorCommand(self, cmd: str, flags: int) -> str: ...  # noqa: N802


class _ReaperVolume(Protocol):
    def delete(self, flags: int = 0) -> int: ...


class _ReaperPool(Protocol):
    def refresh(self, flags: int = 0) -> int: ...
    def storageVolLookupByName(self, name: str) -> _ReaperVolume: ...  # noqa: N802


class _ReaperConn(Protocol):
    def lookupByName(self, name: str) -> _ReaperDomain: ...  # noqa: N802
    def storagePoolLookupByName(self, name: str) -> _ReaperPool: ...  # noqa: N802
    def close(self) -> object: ...


type OpenCaptureReaperConnection = Callable[[str], _ReaperConn]


class RemoteLibvirtCaptureReaper:
    """Detach an orphaned capture's filter-dump, then delete its pcap volume (ADR-0556)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_for_resource: Callable[[str], RemoteLibvirtConfig] = remote_config_for_resource,
        open_connection: Callable[[str], _ReaperConn] = open_libvirt_reaper,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config_for_resource = config_for_resource
        self._open_connection = open_connection
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtCaptureReaper:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    async def reclaim_capture(self, capture: OrphanedCapture) -> bool:
        """Detach the capture's filter, then delete its volume; ``True`` when nothing is left.

        Raises:
            CategorizedError: ``TRANSPORT_FAILURE`` when the bound host is unreachable (the
                ADR-0565 gate) or the TLS connect fails, ``CONTROL_FAILURE`` for a domain or
                monitor error other than an absence, and ``INFRASTRUCTURE_FAILURE`` for a
                pool/volume error other than an absence. The sweep defers the row on both the
                raises and a ``False`` decline; this implementation only returns ``True`` —
                every non-success path either tolerates an absence or raises.
        """
        return await asyncio.to_thread(self._reclaim_blocking, capture)

    def _reclaim_blocking(self, capture: OrphanedCapture) -> bool:
        config = self._config_for_resource(capture.resource_name)
        with remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        ) as conn:
            self._detach_filter(conn, capture)
            self._delete_volume(conn, config.storage_pool, capture)
        return True

    def _detach_filter(self, conn: _ReaperConn, capture: OrphanedCapture) -> None:
        """``object-del`` the capture's filter on its domain, tolerating either absence."""
        qom_id = capture_qom_id(capture.job_id)
        try:
            domain = conn.lookupByName(capture.domain_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                _log.info(
                    "reconciler: capture domain %s is gone; no filter left to detach",
                    capture.domain_name,
                )
                return
            raise _control("looking up", capture.domain_name) from exc
        cmd = {"execute": "object-del", "arguments": {"id": qom_id}}
        try:
            domain.qemuMonitorCommand(json.dumps(cmd), 0)
        except libvirt.libvirtError as exc:
            if _is_not_found(exc):
                _log.info(
                    "reconciler: capture filter %s already absent on %s; continuing",
                    qom_id,
                    capture.domain_name,
                )
                return
            raise _control("removing capture filter on", capture.domain_name) from exc

    def _delete_volume(
        self, conn: _ReaperConn, storage_pool: str, capture: OrphanedCapture
    ) -> None:
        """Delete the job's pcap volume from the pool, tolerating its absence."""
        vol_name = pcap_volume_name(capture.system_id, capture.job_id)
        try:
            pool = conn.storagePoolLookupByName(storage_pool)
        except libvirt.libvirtError as exc:
            raise _infra("looking up storage pool", pool=storage_pool) from exc
        try:
            pool.refresh(0)
        except libvirt.libvirtError as exc:
            raise _infra("refreshing storage pool", pool=storage_pool) from exc
        try:
            volume = pool.storageVolLookupByName(vol_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                _log.info("reconciler: capture volume %s already absent", vol_name)
                return
            raise _infra("looking up capture volume", volume=vol_name) from exc
        try:
            volume.delete(0)
        except libvirt.libvirtError as exc:
            raise _infra("deleting capture volume", volume=vol_name) from exc
        _log.info("reconciler: deleted orphaned capture volume %s", vol_name)


def _is_not_found(exc: libvirt.libvirtError) -> bool:
    """A QMP ``object-del`` on a missing id yields "object 'X' not found" / ``DeviceNotFound``.

    Same QMP semantics as the lifecycle capturer's check; kept local so the reaper slice stays
    independent of the capture path's wider connection slice (ADR-0076).
    """
    message = str(exc).lower()
    return "not found" in message or "devicenotfound" in message


def _control(verb: str, domain_name: str) -> CategorizedError:
    return CategorizedError(
        f"remote libvirt error {verb} domain",
        category=ErrorCategory.CONTROL_FAILURE,
        details={"domain": domain_name},
    )


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )


__all__ = ["OpenCaptureReaperConnection", "RemoteLibvirtCaptureReaper"]
