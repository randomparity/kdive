"""Local-libvirt reconciler reapers (ADR-0111 infra; ADR-0556/0567 capture).

Realizes the reconciler's :class:`~kdive.providers.infra.reaping.InfraReaper` port over the
local-libvirt discovery + provisioning planes, so the periodic ``leaked_domains`` sweep
actually reaches the local host's domains. ``list_owned`` adapts each
:class:`~kdive.providers.ports.OwnedInfra` row (``{system_id: str, domain_name: str}``) into
the reconciler's ``OwnedDomain`` shape (``name`` + ``system_id: UUID | None``); an empty or
unparseable ``system_id`` becomes ``None`` (never ``UUID("")``, which raises) so the
reconciler falls back to the deterministic name to resolve a genuinely orphaned domain.
``destroy`` routes to the provisioning teardown (destroy + undefine + overlay reclaim),
idempotent over an already-absent domain. Both ports are synchronous, so the blocking calls
are offloaded with :func:`asyncio.to_thread`. Construction is lazy (``from_env`` opens no
connection), so the reaper is safe to assemble unconditionally.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import libvirt

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import OrphanedCapture, OwnedDomain
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.ports.handles import OwnedInfra
from kdive.providers.ports.traffic import capture_qom_id
from kdive.providers.shared.runtime_paths import pcap_path


@dataclass(frozen=True, slots=True)
class _OwnedDomain:
    """The reconciler ``OwnedDomain`` shape (``name`` + optional System id)."""

    name: str
    system_id: UUID | None


class _Discovery(Protocol):
    def list_owned(self) -> list[OwnedInfra]: ...


class _Provisioning(Protocol):
    def teardown(self, domain_name: str) -> None: ...


def _uuid_or_none(value: str) -> UUID | None:
    """Parse ``value`` to a ``UUID``; an empty or invalid string is ``None`` (never raises)."""
    try:
        return UUID(value)
    except ValueError:
        return None


def _to_owned_domain(infra: OwnedInfra) -> OwnedDomain:
    return _OwnedDomain(
        name=infra["domain_name"],
        system_id=_uuid_or_none(infra["system_id"]),
    )


class LibvirtInfraReaper:
    """The reconciler ``InfraReaper`` port backed by the local-libvirt provider ports."""

    def __init__(self, *, discovery: _Discovery, provisioning: _Provisioning) -> None:
        self._discovery = discovery
        self._provisioning = provisioning

    @classmethod
    def from_env(cls) -> LibvirtInfraReaper:
        """Build from the local-libvirt env; opens no connection here."""
        return cls(
            discovery=LocalLibvirtDiscovery.from_env(),
            provisioning=LocalLibvirtProvisioning.from_env(),
        )

    async def list_owned(self) -> list[OwnedDomain]:
        """List the host's kdive-owned domains in the reconciler ``OwnedDomain`` shape."""
        infra = await asyncio.to_thread(self._discovery.list_owned)
        return [_to_owned_domain(item) for item in infra]

    async def destroy(self, name: str) -> None:
        """Destroy + undefine the domain (and reclaim its overlay); idempotent if absent."""
        await asyncio.to_thread(self._provisioning.teardown, name)


_log = logging.getLogger(__name__)


class _CaptureConn(Protocol):
    def lookupByName(self, name: str) -> object: ...  # noqa: N802 - libvirt binding name
    def close(self) -> int: ...  # noqa: N802 - libvirt binding name


type CaptureConnect = Callable[[], _CaptureConn]
type CaptureMonitor = Callable[[object, str, int], str]


def _capture_is_not_found(exc: libvirt.libvirtError) -> bool:
    """A QMP ``object-del`` on a missing id yields "object 'X' not found" / DeviceNotFound.

    Deliberate duplicate of the live capturer's matcher: QMP passthrough errors carry no
    distinct ``VIR_ERR_*`` code, so absence is matched on lowercased message text.
    """
    message = str(exc).lower()
    return "not found" in message or "devicenotfound" in message


def _close_capture_conn(conn: _CaptureConn) -> None:
    """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


class LocalLibvirtCaptureReaper:
    """Detach an orphaned capture's filter-dump, then unlink its worker-local pcap.

    Reconciler-side by ADR-0567: the pcap is a fixed absolute path on the kdive host
    (``pcap_path``), the provider host *is* the kdive host, and the root reconciler already
    drives local-host reapers over the same connection. Idempotent per the ``CaptureReaper``
    port: an already-missing domain, filter, or file is tolerated; any other failure raises
    so the sweep defers the row and retries.
    """

    def __init__(self, *, connect: CaptureConnect, monitor: CaptureMonitor) -> None:
        self._connect = connect
        self._monitor = monitor

    @classmethod
    def from_env(cls) -> LocalLibvirtCaptureReaper:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
        # Lazy import keeps the QEMU-specific binding off the module import path (mirrors
        # lifecycle/traffic_capture.py), so unit tests inject a fake ``monitor`` instead.
        import libvirt_qemu

        host_uri = config.require(LIBVIRT_URI)
        return cls(
            connect=lambda: libvirt.open(host_uri),
            monitor=libvirt_qemu.qemuMonitorCommand,
        )

    async def reclaim_capture(self, capture: OrphanedCapture) -> bool:
        """Detach the capture's filter, then unlink its pcap; ``True`` when nothing is left.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` for a connect, domain-lookup, or monitor
                error other than an absence, ``INFRASTRUCTURE_FAILURE`` for an unlink failure
                other than absence. The sweep defers the row on both; this implementation only
                returns ``True`` — every non-success path either tolerates an absence or raises.
        """
        return await asyncio.to_thread(self._reclaim_blocking, capture)

    def _reclaim_blocking(self, capture: OrphanedCapture) -> bool:
        conn = self._open()
        try:
            self._detach_filter(conn, capture)
        finally:
            _close_capture_conn(conn)
        self._unlink_pcap(capture)
        return True

    def _open(self) -> _CaptureConn:
        try:
            return self._connect()
        except libvirt.libvirtError as exc:
            raise self._control_failure("connecting to libvirt for", "capture") from exc

    def _detach_filter(self, conn: _CaptureConn, capture: OrphanedCapture) -> None:
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
            raise self._control_failure("looking up", capture.domain_name) from exc
        cmd = {"execute": "object-del", "arguments": {"id": qom_id}}
        try:
            self._monitor(domain, json.dumps(cmd), 0)
        except libvirt.libvirtError as exc:
            if _capture_is_not_found(exc):
                _log.info(
                    "reconciler: capture filter %s already absent on %s; continuing",
                    qom_id,
                    capture.domain_name,
                )
                return
            raise self._control_failure("removing capture filter on", capture.domain_name) from exc

    def _unlink_pcap(self, capture: OrphanedCapture) -> None:
        """Remove the job's pcap file, tolerating absence; any other OSError raises."""
        path = pcap_path(capture.system_id, capture.job_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise CategorizedError(
                f"could not remove orphaned capture pcap {path}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"path": str(path)},
            ) from exc
        _log.info("reconciler: removed orphaned capture pcap %s", path)

    @staticmethod
    def _control_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.CONTROL_FAILURE,
            details={"domain": domain_name},
        )
