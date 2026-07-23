"""Local-libvirt traffic capture: QEMU filter-dump on a running guest's netdev (ADR-0385).

Attaches/removes a ``filter-dump`` netfilter object via the libvirt QMP passthrough
(``libvirt_qemu.qemuMonitorCommand``) keyed on the domain name. DB-free — the worker handler owns
the bounded size-poll and cancellation. The leading ``object-del`` tolerates not-found so the
first-ever capture (no stale filter) succeeds and an at-least-once retry re-attaches idempotently.
QMP passthrough errors surface as a generic ``libvirt.libvirtError`` string with no distinct
``VIR_ERR_*`` code, so the not-found tolerance matches on the QMP error message/class text (unlike
the typed ``control._idempotent`` / ``snapshot._delete_if_exists`` code-based swallows).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.xml import SYSTEM_SSH_NETDEV_ID
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.ports.traffic import TrafficCapturer as TrafficCapturer
from kdive.providers.shared.runtime_paths import (
    PCAP_HYPERVISOR_WRITE_REMEDIATION,
    pcap_path,
    prepare_pcap_dir,
    read_pcap_bytes,
)


class _LibvirtConn(Protocol):
    def lookupByName(self, name: str) -> object: ...
    def close(self) -> int: ...


type Connect = Callable[[], _LibvirtConn]
type Monitor = Callable[[object, str, int], str]

_log = logging.getLogger(__name__)


def _close(conn: _LibvirtConn) -> None:
    """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


def _is_not_found(exc: libvirt.libvirtError) -> bool:
    """A QMP ``object-del`` on a missing id yields "object 'X' not found" / ``DeviceNotFound``."""
    message = str(exc).lower()
    return "not found" in message or "devicenotfound" in message


class LocalLibvirtTrafficCapture:
    """The `TrafficCapturer` for the local libvirt host (filter-dump attach/detach)."""

    def __init__(self, *, connect: Connect, monitor: Monitor) -> None:
        self._connect = connect
        self._monitor = monitor

    @classmethod
    def from_env(cls) -> LocalLibvirtTrafficCapture:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
        # Lazy import keeps the QEMU-specific binding off the module import path (mirrors
        # transport_reset.py / guest/agent.py), so unit tests inject a fake ``monitor`` instead.
        import libvirt_qemu

        host_uri = config.require(LIBVIRT_URI)
        return cls(
            connect=lambda: libvirt.open(host_uri),
            monitor=libvirt_qemu.qemuMonitorCommand,
        )

    @property
    def write_remediation(self) -> str:
        """Operator guidance when the qemu:///system hypervisor could not write the pcap dir."""
        return PCAP_HYPERVISOR_WRITE_REMEDIATION

    def prepare(self, system_id: UUID, job_id: UUID) -> str:
        """Prepare the QEMU-writable per-System pcap dir and return the worker pcap path.

        The confined qemu:///system hypervisor writes the filter-dump as the QEMU runtime user, so
        the dir is owned to that user and SELinux-labelled ``svirt_image_t`` (ADR-0385); a genuine
        write failure surfaces loudly at :meth:`fetch` via a short/absent file.
        """
        prepare_pcap_dir(system_id)
        return str(pcap_path(system_id, job_id))

    def attach(self, domain_name: str, *, qom_id: str, dest_path: str, snaplen: int) -> None:
        """Add a filter-dump on the SSH-forward netdev writing ``dest_path`` (idempotent re-attach).

        The captured netdev is the local-libvirt SSH-forward netdev (``SYSTEM_SSH_NETDEV_ID``), a
        provider-internal XML detail; the handler never names it.
        """
        conn = self._open()
        try:
            domain = self._lookup(conn, domain_name)
            self._object_del(domain, domain_name, qom_id, tolerate_missing=True)
            self._object_add(
                domain,
                domain_name,
                {
                    "qom-type": "filter-dump",
                    "id": qom_id,
                    "netdev": SYSTEM_SSH_NETDEV_ID,
                    "file": dest_path,
                    "maxlen": snaplen,
                },
            )
        finally:
            _close(conn)

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        """Remove the filter-dump ``qom_id`` (tolerating not-found)."""
        conn = self._open()
        try:
            domain = self._lookup(conn, domain_name)
            self._object_del(domain, domain_name, qom_id, tolerate_missing=True)
        finally:
            _close(conn)

    def captured_size(self, dest_path: str) -> int:
        """Current size of the growing local pcap (0 until the hypervisor writes the header)."""
        path = Path(dest_path)
        return path.stat().st_size if path.exists() else 0

    def fetch(self, dest_path: str, *, max_bytes: int) -> bytes:
        """Read the local pcap whole; an absent capture is empty (the ADR-0223 wall raises).

        ``max_bytes`` is unused: the local file is already bounded by the handler's poll loop and
        the filter-dump ``maxlen``; it exists only so the remote provider can bound its download.
        """
        del max_bytes
        return read_pcap_bytes(Path(dest_path))

    def reclaim(self, dest_path: str) -> None:
        """Best-effort delete of the local pcap file; never masks the handler's real result."""
        with contextlib.suppress(OSError):
            Path(dest_path).unlink(missing_ok=True)

    def _object_add(self, domain: object, domain_name: str, arguments: dict[str, object]) -> None:
        cmd = {"execute": "object-add", "arguments": arguments}
        try:
            self._monitor(domain, json.dumps(cmd), 0)
        except libvirt.libvirtError as exc:
            raise self._control_failure("adding capture filter on", domain_name) from exc

    def _object_del(
        self, domain: object, domain_name: str, qom_id: str, *, tolerate_missing: bool
    ) -> None:
        cmd = {"execute": "object-del", "arguments": {"id": qom_id}}
        try:
            self._monitor(domain, json.dumps(cmd), 0)
        except libvirt.libvirtError as exc:
            if tolerate_missing and _is_not_found(exc):
                _log.info("capture filter %s already absent on %s; continuing", qom_id, domain_name)
                return
            raise self._control_failure("removing capture filter on", domain_name) from exc

    def _open(self) -> _LibvirtConn:
        try:
            return self._connect()
        except libvirt.libvirtError as exc:
            raise self._control_failure("connecting to libvirt for", "capture") from exc

    def _lookup(self, conn: _LibvirtConn, domain_name: str) -> object:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise self._control_failure("looking up", domain_name) from exc

    @staticmethod
    def _control_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.CONTROL_FAILURE,
            details={"domain": domain_name},
        )
