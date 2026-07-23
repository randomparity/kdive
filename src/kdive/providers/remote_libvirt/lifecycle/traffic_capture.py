"""Remote-libvirt traffic capture: QEMU filter-dump over qemu+tls with pcap fetch-back (ADR-0432).

Realizes the `kdive.providers.ports.traffic.TrafficCapturer` port against the remote host, the
deferred ADR-0385 follow-up (#1434). The capture mechanic matches local-libvirt — a ``filter-dump``
netfilter object attached via the libvirt QMP passthrough (``qemuMonitorCommand``) — but three
things differ, exactly as ADR-0385 anticipated:

- **Netdev discovery.** Local hardcodes its SSH-forward netdev id; remote discovers libvirt's
  auto-generated netdev id (``hostnetN``) from the running domain's XML interface alias at runtime.
- **Write location.** The pcap is written on the *remote* host, into the operator ``storage_pool``
  directory (already QEMU-writable — it holds the domain's disk images), under a deterministic
  per-job volume name.
- **Fetch-back.** After the capture window the pcap is streamed back to the worker over the same
  ``qemu+tls`` connection via ``volume.download`` + ``stream.recvAll`` — the mechanism remote
  host_dump already uses — bounded so a runaway file cannot exhaust worker memory.

No shared code layer with local-libvirt (ADR-0076); the connection lifecycle is the shared
mutual-TLS ``remote_connection`` (ADR-0077), exactly as every other remote-libvirt port. The
blocking libvirt calls run only under the ``live_vm`` gate; orchestration, netdev discovery, and
the bounded sink are unit-tested with fakes. Reclaim is guaranteed on every worker-driven exit by
the handler's ``finally``; :meth:`prepare` additionally pre-deletes *this job's own* stale volume
(concurrency-safe, unlike a whole-System sweep), so an at-least-once retry of a job that died
mid-capture starts clean. A pcap orphaned by a job that exhausts its retries is reclaimed by the
reconciler's volume reaper (a noted follow-up).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.traffic import TrafficCapturer as TrafficCapturer
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, unbound_remote_config
from kdive.providers.remote_libvirt.connection.transport import (
    open_libvirt_protocol,
    remote_connection,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)

# A remote System's traffic rides a libvirt-managed dir/fs storage pool; only those pool types
# expose a host directory the filter-dump can write into and libvirt can then list as a volume.
_DIR_POOL_TYPES = frozenset({"dir", "fs", "netfs"})
# The deterministic per-job pcap volume name carries the owning System (for the stale sweep) and
# the job (uniqueness). ``kdive-pcap-<system_id>-<job_id>.pcap``.
_PCAP_VOLUME_SUFFIX = ".pcap"

# Operator guidance when the remote hypervisor could not write the capture pcap into the pool.
REMOTE_PCAP_WRITE_REMEDIATION = (
    "the remote qemu:///system hypervisor could not write the capture pcap into the operator "
    "storage_pool; ensure the pool is a filesystem/dir pool whose directory the remote QEMU "
    "runtime user can create files in (the pool already backs the domain's disk images, so this "
    "usually indicates a full or read-only pool filesystem)"
)


def pcap_volume_name(system_id: UUID, job_id: UUID) -> str:
    """The deterministic per-job pcap volume filename inside the storage pool."""
    return f"kdive-pcap-{system_id}-{job_id}{_PCAP_VOLUME_SUFFIX}"


class _CaptureVolume(Protocol):
    def name(self) -> str: ...
    def info(self) -> list[int]: ...
    def download(self, stream: Any, offset: int, length: int, flags: int) -> int: ...
    def delete(self, flags: int = 0) -> int: ...


class _CapturePool(Protocol):
    def refresh(self, flags: int = 0) -> int: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - libvirt binding name
    def storageVolLookupByName(self, name: str) -> _CaptureVolume: ...  # noqa: N802


class _CaptureDomain(Protocol):
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802 - libvirt binding name
    def qemuMonitorCommand(self, cmd: str, flags: int) -> str: ...  # noqa: N802


class _CaptureConn(Protocol):
    def lookupByName(self, name: str) -> _CaptureDomain: ...  # noqa: N802 - libvirt binding name
    def storagePoolLookupByName(self, name: str) -> _CapturePool: ...  # noqa: N802
    def newStream(self, flags: int = 0) -> Any: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenCaptureConnection = Callable[[str], _CaptureConn]


def open_libvirt_capture(uri: str) -> _CaptureConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


def discover_netdev_id(domain_xml: str) -> str:
    """The QEMU netdev id of the guest's first aliased interface (``hostnetN``).

    libvirt's ``qemuAliasHostnetFromDevice`` builds the ``-netdev`` id by prepending ``host`` to the
    device alias, so an interface aliased ``net0`` is netdev ``hostnet0``. A multi-NIC guest
    captures only its first data-plane interface.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the domain XML is malformed or exposes no
            aliased interface (nothing to capture).
    """
    try:
        root = _safe_fromstring(domain_xml)
    except Exception as exc:  # noqa: BLE001 - host-emitted XML; a parse failure reads as config
        raise CategorizedError(
            "remote domain XML could not be parsed for netdev discovery",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc
    for interface in root.findall("./devices/interface"):
        alias = interface.find("./alias")
        alias_name = alias.get("name") if alias is not None else None
        if alias_name:
            return f"host{alias_name}"
    raise CategorizedError(
        "remote domain exposes no aliased network interface to capture",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


class _BoundedMemorySink:
    """Accumulate a downloaded pcap in memory, aborting if it exceeds the fetch ceiling."""

    def __init__(self, ceiling_bytes: int) -> None:
        self._buf = bytearray()
        self._ceiling = ceiling_bytes

    def recv(self, _stream: Any, data: bytes, _opaque: Any) -> None:
        self._buf.extend(data)
        if len(self._buf) > self._ceiling:
            raise CategorizedError(
                "remote traffic capture exceeded the fetch ceiling mid-download",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"ceiling_bytes": str(self._ceiling)},
            )

    def data(self) -> bytes:
        return bytes(self._buf)


class RemoteLibvirtTrafficCapture:
    """The `TrafficCapturer` for the remote libvirt host (filter-dump + storage-volume fetch-back).

    The fetch ceiling is a memory safety valve, not the capture bound (the handler's poll loop is):
    a well-behaved capture ends at ~``max_bytes``; the ``2×`` ceiling only trips if the file grew
    unbounded despite the poll.
    """

    _FETCH_CEILING_FACTOR = 2

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
        open_connection: OpenCaptureConnection = open_libvirt_capture,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir

    @classmethod
    def from_env(
        cls,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
    ) -> RemoteLibvirtTrafficCapture:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry, config_factory=config_factory)

    @property
    def write_remediation(self) -> str:
        """Operator guidance when the remote hypervisor could not write the pcap into the pool."""
        return REMOTE_PCAP_WRITE_REMEDIATION

    def prepare(self, system_id: UUID, job_id: UUID) -> str:
        """Pre-delete this job's stale pcap volume and return the remote pool pcap path.

        The pre-delete is keyed on this job's own deterministic volume name (not a whole-System
        sweep, which would nuke a concurrent capture on the same System), so an at-least-once retry
        of a job whose prior attempt died mid-capture starts from a clean volume. The returned path
        is the ``filter-dump`` ``file=`` target on the remote host. A pcap orphaned by a job that
        exhausts its retries is reclaimed by the reconciler's volume reaper (a noted follow-up), not
        here.
        """
        vol_name = pcap_volume_name(system_id, job_id)
        with self._connection() as conn:
            pool_dir = self._pool_dir(conn)
            self._delete_stale_volume(conn, vol_name)
            return str(PurePosixPath(pool_dir) / vol_name)

    def attach(self, domain_name: str, *, qom_id: str, dest_path: str, snaplen: int) -> None:
        """Discover the netdev, then add a filter-dump writing ``dest_path`` (idempotent)."""
        with self._connection() as conn:
            domain = self._lookup_domain(conn, domain_name)
            netdev = discover_netdev_id(self._domain_xml(domain, domain_name))
            self._object_del(domain, domain_name, qom_id, tolerate_missing=True)
            self._object_add(
                domain,
                domain_name,
                {
                    "qom-type": "filter-dump",
                    "id": qom_id,
                    "netdev": netdev,
                    "file": dest_path,
                    "maxlen": snaplen,
                },
            )

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        """Remove the filter-dump ``qom_id`` (tolerating not-found)."""
        with self._connection() as conn:
            domain = self._lookup_domain(conn, domain_name)
            self._object_del(domain, domain_name, qom_id, tolerate_missing=True)

    def captured_size(self, dest_path: str) -> int:
        """Current byte size of the growing remote pcap volume (0 if not yet written)."""
        vol_name = PurePosixPath(dest_path).name
        with self._connection() as conn:
            pool = self._lookup_pool(conn)
            self._refresh_pool(pool)
            volume = self._lookup_volume_optional(pool, vol_name)
            if volume is None:
                return 0
            return self._volume_capacity(volume, vol_name)

    def fetch(self, dest_path: str, *, max_bytes: int) -> bytes:
        """Stream the remote pcap volume back to worker memory; an absent volume is empty bytes."""
        vol_name = PurePosixPath(dest_path).name
        ceiling = max(1, max_bytes) * self._FETCH_CEILING_FACTOR
        with self._connection() as conn:
            pool = self._lookup_pool(conn)
            self._refresh_pool(pool)
            volume = self._lookup_volume_optional(pool, vol_name)
            if volume is None:
                return b""
            return self._download(conn, volume, vol_name, ceiling)

    def reclaim(self, dest_path: str) -> None:
        """Best-effort delete of the remote pcap volume; never masks the handler's real result."""
        vol_name = PurePosixPath(dest_path).name
        try:
            with self._connection() as conn:
                pool = self._lookup_pool(conn)
                volume = self._lookup_volume_optional(pool, vol_name)
                if volume is not None:
                    _delete_volume(volume)
        except CategorizedError:
            _log.warning("remote pcap reclaim failed for %s; volume may linger", vol_name)

    def _connection(self) -> AbstractContextManager[_CaptureConn]:
        return remote_connection(
            self._config_factory(),
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    def _pool_dir(self, conn: _CaptureConn) -> str:
        pool = self._lookup_pool(conn)
        try:
            pool_xml = pool.XMLDesc(0)
        except libvirt.libvirtError as exc:
            raise self._infra("reading storage-pool XML for") from exc
        pool_type, target = _pool_type_and_target(pool_xml)
        if pool_type not in _DIR_POOL_TYPES or target is None:
            raise CategorizedError(
                "remote storage_pool is not a filesystem/dir pool; traffic capture requires one",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"pool_type": pool_type or "unknown"},
            )
        return target

    def _delete_stale_volume(self, conn: _CaptureConn, vol_name: str) -> None:
        pool = self._lookup_pool(conn)
        self._refresh_pool(pool)
        volume = self._lookup_volume_optional(pool, vol_name)
        if volume is not None:
            _log.info("reclaiming stale pcap volume %s before capture", vol_name)
            _delete_volume(volume)

    def _lookup_pool(self, conn: _CaptureConn) -> _CapturePool:
        pool_name = self._config_factory().storage_pool
        try:
            return conn.storagePoolLookupByName(pool_name)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote storage-pool lookup failed for traffic capture",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"storage_pool": pool_name},
            ) from exc

    @staticmethod
    def _refresh_pool(pool: _CapturePool) -> None:
        try:
            pool.refresh(0)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote storage-pool refresh failed for traffic capture",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc

    @staticmethod
    def _lookup_volume_optional(pool: _CapturePool, vol_name: str) -> _CaptureVolume | None:
        try:
            return pool.storageVolLookupByName(vol_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return None
            raise CategorizedError(
                "remote pcap volume lookup failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"volume": vol_name},
            ) from exc

    @staticmethod
    def _volume_capacity(volume: _CaptureVolume, vol_name: str) -> int:
        try:
            return int(volume.info()[1])
        except (libvirt.libvirtError, TypeError, IndexError, ValueError) as exc:
            raise CategorizedError(
                "remote pcap volume size lookup failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"volume": vol_name},
            ) from exc

    def _download(
        self, conn: _CaptureConn, volume: _CaptureVolume, vol_name: str, ceiling: int
    ) -> bytes:
        stream = conn.newStream(0)
        sink = _BoundedMemorySink(ceiling)
        try:
            volume.download(stream, 0, 0, 0)
            stream.recvAll(sink.recv, None)
            stream.finish()
        except CategorizedError:
            with contextlib.suppress(Exception):
                stream.abort()
            raise
        except (libvirt.libvirtError, OSError, RuntimeError) as exc:
            with contextlib.suppress(Exception):
                stream.abort()
            raise CategorizedError(
                "remote pcap stream download failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"volume": vol_name},
            ) from exc
        return sink.data()

    def _lookup_domain(self, conn: _CaptureConn, domain_name: str) -> _CaptureDomain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise self._control_failure("looking up", domain_name) from exc

    def _domain_xml(self, domain: _CaptureDomain, domain_name: str) -> str:
        try:
            return domain.XMLDesc(0)
        except libvirt.libvirtError as exc:
            raise self._control_failure("reading XML of", domain_name) from exc

    def _object_add(
        self, domain: _CaptureDomain, domain_name: str, arguments: dict[str, object]
    ) -> None:
        cmd = {"execute": "object-add", "arguments": arguments}
        try:
            domain.qemuMonitorCommand(json.dumps(cmd), 0)
        except libvirt.libvirtError as exc:
            raise self._control_failure("adding capture filter on", domain_name) from exc

    def _object_del(
        self, domain: _CaptureDomain, domain_name: str, qom_id: str, *, tolerate_missing: bool
    ) -> None:
        cmd = {"execute": "object-del", "arguments": {"id": qom_id}}
        try:
            domain.qemuMonitorCommand(json.dumps(cmd), 0)
        except libvirt.libvirtError as exc:
            if tolerate_missing and _is_not_found(exc):
                _log.info("capture filter %s already absent on %s; continuing", qom_id, domain_name)
                return
            raise self._control_failure("removing capture filter on", domain_name) from exc

    @staticmethod
    def _control_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"remote libvirt error {verb} domain",
            category=ErrorCategory.CONTROL_FAILURE,
            details={"domain": domain_name},
        )

    def _infra(self, verb: str) -> CategorizedError:
        return CategorizedError(
            f"remote libvirt error {verb} traffic capture",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"storage_pool": self._config_factory().storage_pool},
        )


def _is_not_found(exc: libvirt.libvirtError) -> bool:
    """A QMP ``object-del`` on a missing id yields "object 'X' not found" / ``DeviceNotFound``."""
    message = str(exc).lower()
    return "not found" in message or "devicenotfound" in message


def _delete_volume(volume: _CaptureVolume) -> None:
    with contextlib.suppress(Exception):
        volume.delete(0)


def _pool_type_and_target(pool_xml: str) -> tuple[str | None, str | None]:
    try:
        root = _safe_fromstring(pool_xml)
    except Exception:  # noqa: BLE001 - host-emitted XML; a parse failure reads as unknown
        return None, None
    return root.get("type"), root.findtext("./target/path")


__all__ = [
    "RemoteLibvirtTrafficCapture",
    "REMOTE_PCAP_WRITE_REMEDIATION",
    "discover_netdev_id",
    "pcap_volume_name",
]
