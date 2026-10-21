"""Remote-libvirt traffic capture: QEMU filter-dump over qemu+tls with pcap fetch-back (ADR-0432).

Realizes the `kdive.providers.ports.traffic.TrafficCapturer` port against the remote host, the
deferred ADR-0385 follow-up (#1434). The capture mechanic matches local-libvirt — a ``filter-dump``
netfilter object attached via the libvirt QMP passthrough (``qemuMonitorCommand``) — but three
things differ, exactly as ADR-0385 anticipated:

- **Netdev discovery.** Local hardcodes its SSH-forward netdev id; remote discovers libvirt's
  auto-generated netdev id (``hostnetN``) from the running domain's XML interface alias at runtime.
- **Write location.** The pcap is written on the *remote* host, into the dedicated ``pcap_pool``
  directory that ``site.yml`` provisions root:root 0711 (ADR-0432 amendment, #3316), under a
  deterministic per-job volume name the provider pre-creates for the domain's QEMU user.
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
reconciler's capture reaper (ADR-0556).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import xml.etree.ElementTree as ET  # noqa: S405 - builds XML only; parsing uses defusedxml
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.traffic import TrafficCapturer as TrafficCapturer
from kdive.providers.ports.traffic import pcap_volume_name
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, unbound_remote_config
from kdive.providers.remote_libvirt.connection.monitor import qemu_monitor_command
from kdive.providers.remote_libvirt.connection.transport import (
    open_libvirt_protocol,
    remote_connection,
)
from kdive.providers.shared.qmp_reply import MalformedQmpReplyError, QmpReply, parse_qmp_reply
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)
# QEMU-controlled text reaches operator logs only, bounded and repr-quoted.
_LOGGED_DESCRIPTION_CHARS = 256

# The pcap pool must be a libvirt-managed dir/fs pool; only those pool types expose a host
# directory the filter-dump can write into and libvirt can then list as a volume.
_DIR_POOL_TYPES = frozenset({"dir", "fs", "netfs"})
# The deterministic per-job pcap volume name convention lives in providers.ports.traffic beside
# capture_qom_id: the capturer and the ADR-0556 reaper must spell the destination identically,
# and the gated-child overlay shadows this module, so the shared convention cannot live here.

# Operator guidance when the remote hypervisor has no pcap pool or could not create the pcap in it.
REMOTE_PCAP_WRITE_REMEDIATION = (
    "rerun deploy/ansible site.yml on the provider host: it provisions the dir pool named by "
    "KDIVE_REMOTE_LIBVIRT_PCAP_POOL (default kdive-pcap; another name must match the "
    "libvirt_pool_net_pcap_pool_name variable). Its directory must be root:root 0711 and "
    "searchable by the QEMU runtime user, labelled svirt_image_t on an SELinux-enforcing host or "
    "allowed by the libvirt-qemu.d AppArmor drop-in, and on a filesystem neither full nor "
    "read-only"
)
# Operator guidance when the running domain's DAC identity cannot own the pre-created pcap.
REMOTE_PCAP_DAC_REMEDIATION = (
    "the remote domain's live XML carries no usable DAC seclabel (<seclabel model='dac'> with a "
    "<label> of +uid:+gid for a non-root QEMU user); keep libvirt's DAC security driver and an "
    "unprivileged qemu.conf user, then reprovision the System"
)
# libvirt renders the DAC seclabel of a running domain as "+uid:+gid".
_DAC_LABEL = re.compile(r"\+([0-9]{1,10}):\+([0-9]{1,10})")
# The pre-created pcap is the QEMU user's alone; the root-owned pool directory stops QEMU from
# unlinking or replacing it, so the root fetch-back never follows a QEMU-made link (#3316).
_PCAP_FILE_MODE = "0600"

# Operator guidance when the System's data NIC was defined with vhost-net, which QEMU never filters.
REMOTE_PCAP_VHOST_REMEDIATION = (
    "this System's data NIC was defined with vhost-net, on which QEMU refuses every capture "
    "filter; reprovision the System so its NIC renders without vhost"
)


class _CaptureVolume(Protocol):
    def name(self) -> str: ...
    def info(self) -> list[int]: ...
    def download(self, stream: Any, offset: int, length: int, flags: int) -> int: ...
    def delete(self, flags: int = 0) -> int: ...


class _CapturePool(Protocol):
    def isActive(self) -> int: ...  # noqa: N802 - libvirt binding name
    def createXML(self, xml_desc: str, flags: int = 0, /) -> _CaptureVolume: ...  # noqa: N802
    def refresh(self, flags: int = 0) -> int: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - libvirt binding name
    def storageVolLookupByName(self, name: str) -> _CaptureVolume: ...  # noqa: N802


class _CaptureDomain(Protocol):
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802 - libvirt binding name


class _CaptureConn(Protocol):
    def lookupByName(self, name: str) -> _CaptureDomain: ...  # noqa: N802 - libvirt binding name
    def storagePoolLookupByName(self, name: str) -> _CapturePool: ...  # noqa: N802
    def newStream(self, flags: int = 0) -> Any: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenCaptureConnection = Callable[[str], _CaptureConn]


def open_libvirt_capture(uri: str) -> _CaptureConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


def discover_dac_identity(domain_xml: str) -> tuple[int, int]:
    """The ``(uid, gid)`` the running domain's QEMU process runs as, from its DAC seclabel.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` (``reason=dac_seclabel_unusable``) when the XML
            is malformed or carries no ``+uid:+gid`` DAC label for a non-root user. The owner is
            never guessed: a wrong one leaves QEMU unable to write or lets another account in.
    """
    try:
        root = _safe_fromstring(domain_xml)
    except Exception:  # noqa: BLE001 - host-emitted XML; a parse failure reads as unusable
        root = None
    label = None if root is None else root.findtext("./seclabel[@model='dac']/label")
    match = _DAC_LABEL.fullmatch(label.strip()) if label else None
    if match is None or int(match.group(1)) == 0:
        raise CategorizedError(
            "remote domain has no usable DAC identity to own the capture pcap",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "reason": "dac_seclabel_unusable",
                "remediation": REMOTE_PCAP_DAC_REMEDIATION,
            },
        )
    return int(match.group(1)), int(match.group(2))


def capture_volume_xml(name: str, uid: int, gid: int) -> str:
    """An empty raw volume ``name`` owned by ``uid:gid`` with mode 0600."""
    volume = ET.Element("volume")
    ET.SubElement(volume, "name").text = name
    ET.SubElement(volume, "capacity", unit="bytes").text = "0"
    target = ET.SubElement(volume, "target")
    ET.SubElement(target, "format", type="raw")
    permissions = ET.SubElement(target, "permissions")
    ET.SubElement(permissions, "mode").text = _PCAP_FILE_MODE
    ET.SubElement(permissions, "owner").text = str(uid)
    ET.SubElement(permissions, "group").text = str(gid)
    return ET.tostring(volume, encoding="unicode")


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
        exhausts its retries is reclaimed by the reconciler's capture reaper (ADR-0556), not here.
        """
        vol_name = pcap_volume_name(system_id, job_id)
        with self._connection() as conn:
            pool_dir = self._pool_dir(conn)
            self._delete_stale_volume(conn, vol_name)
            return str(PurePosixPath(pool_dir) / vol_name)

    def attach(self, domain_name: str, *, qom_id: str, dest_path: str, snaplen: int) -> None:
        """Pre-create ``dest_path`` for the domain's QEMU user, then add a filter-dump writing it.

        The volume is created before ``object-add`` so QEMU opens an existing file it owns in a
        directory it cannot write; a refused or failed ``object-add`` deletes it again.
        """
        with self._connection() as conn:
            domain = self._lookup_domain(conn, domain_name)
            domain_xml = self._domain_xml(domain, domain_name)
            netdev = discover_netdev_id(domain_xml)
            uid, gid = discover_dac_identity(domain_xml)
            self._object_del(domain, domain_name, qom_id)
            volume = self._create_capture_volume(conn, PurePosixPath(dest_path).name, uid, gid)
            try:
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
            except BaseException:
                _delete_volume(volume)
                raise

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        """Remove the filter-dump ``qom_id`` (tolerating not-found)."""
        with self._connection() as conn:
            domain = self._lookup_domain(conn, domain_name)
            self._object_del(domain, domain_name, qom_id)

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
            raise self._infra("reading pcap pool XML for") from exc
        pool_type, target = _pool_type_and_target(pool_xml)
        if pool_type not in _DIR_POOL_TYPES or target is None:
            raise CategorizedError(
                "remote pcap pool is not a filesystem/dir pool; traffic capture requires one",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"pool_type": pool_type or "unknown"},
            )
        return target

    def _create_capture_volume(
        self, conn: _CaptureConn, vol_name: str, uid: int, gid: int
    ) -> _CaptureVolume:
        self._delete_stale_volume(conn, vol_name)
        pool = self._lookup_pool(conn)
        try:
            return pool.createXML(capture_volume_xml(vol_name, uid, gid), 0)
        except libvirt.libvirtError as exc:
            raise self._infra("creating the pcap volume for") from exc

    def _delete_stale_volume(self, conn: _CaptureConn, vol_name: str) -> None:
        pool = self._lookup_pool(conn)
        self._refresh_pool(pool)
        volume = self._lookup_volume_optional(pool, vol_name)
        if volume is not None:
            _log.info("reclaiming stale pcap volume %s before capture", vol_name)
            _delete_volume(volume)

    def _lookup_pool(self, conn: _CaptureConn) -> _CapturePool:
        pool_name = self._config_factory().pcap_pool
        try:
            pool = conn.storagePoolLookupByName(pool_name)
            active = pool.isActive()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
                raise _pool_unusable(
                    "the remote host has no traffic-capture pcap pool",
                    "pcap_pool_missing",
                    pool_name,
                ) from exc
            raise CategorizedError(
                "remote pcap pool lookup failed for traffic capture",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"pcap_pool": pool_name},
            ) from exc
        if not active:
            # A stopped pool cannot refresh, so every retry would fail the same way.
            raise _pool_unusable(
                "the remote traffic-capture pcap pool is not running",
                "pcap_pool_inactive",
                pool_name,
            )
        return pool

    @staticmethod
    def _refresh_pool(pool: _CapturePool) -> None:
        try:
            pool.refresh(0)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote pcap pool refresh failed for traffic capture",
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
        reply = self._send(domain, domain_name, cmd, "object_add", verb="adding capture filter on")
        if reply.pcap_open_denied:
            raise _configuration_refusal(
                reply,
                domain_name,
                "the remote hypervisor was denied creating the capture pcap",
                reason="pcap_open_denied",
                remediation=REMOTE_PCAP_WRITE_REMEDIATION,
            )
        if reply.vhost_refused:
            raise _configuration_refusal(
                reply,
                domain_name,
                "the System's data NIC is vhost-backed and cannot be captured",
                reason="capture_netdev_vhost",
                remediation=REMOTE_PCAP_VHOST_REMEDIATION,
            )
        if reply.error_class is not None:
            raise _refused(reply, "object_add", domain_name)

    def _object_del(self, domain: _CaptureDomain, domain_name: str, qom_id: str) -> None:
        cmd = {"execute": "object-del", "arguments": {"id": qom_id}}
        reply = self._send(
            domain, domain_name, cmd, "object_del", verb="removing capture filter on"
        )
        if reply.absent:
            _log.info("capture filter %s already absent on %s; continuing", qom_id, domain_name)
            return
        if reply.error_class is not None:
            raise _refused(reply, "object_del", domain_name)

    def _send(
        self,
        domain: _CaptureDomain,
        domain_name: str,
        cmd: Mapping[str, object],
        operation: str,
        *,
        verb: str,
    ) -> QmpReply:
        """Run one QMP command and parse its reply; QEMU refusals arrive as error replies."""
        try:
            raw = qemu_monitor_command(domain, json.dumps(cmd), 0)
        except libvirt.libvirtError as exc:
            raise self._control_failure(verb, domain_name) from exc
        try:
            return parse_qmp_reply(raw)
        except MalformedQmpReplyError as exc:
            raise CategorizedError(
                "QEMU returned a malformed capture filter reply",
                category=ErrorCategory.CONTROL_FAILURE,
                details={
                    "domain": domain_name,
                    "reason": "qmp_reply_malformed",
                    "operation": operation,
                },
            ) from exc

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
            details={"pcap_pool": self._config_factory().pcap_pool},
        )


def _log_refusal(reply: QmpReply, operation: str, domain_name: str) -> None:
    _log.warning(
        "QEMU refused %s on %s: %s %r",
        operation,
        domain_name,
        reply.error_class,
        reply.description[:_LOGGED_DESCRIPTION_CHARS],
    )


def _pool_unusable(message: str, reason: str, pool_name: str) -> CategorizedError:
    return CategorizedError(
        message,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "reason": reason,
            "pcap_pool": pool_name,
            "remediation": REMOTE_PCAP_WRITE_REMEDIATION,
        },
    )


def _configuration_refusal(
    reply: QmpReply, domain_name: str, message: str, *, reason: str, remediation: str
) -> CategorizedError:
    """A refused ``object-add`` no retry can fix; only the class and reason reach details."""
    _log_refusal(reply, "object_add", domain_name)
    return CategorizedError(
        message,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "reason": reason,
            "operation": "object_add",
            "error": str(reply.error_class),
            "remediation": remediation,
        },
    )


def _refused(reply: QmpReply, operation: str, domain_name: str) -> CategorizedError:
    _log_refusal(reply, operation, domain_name)
    return CategorizedError(
        "QEMU refused the capture filter command",
        category=ErrorCategory.CONTROL_FAILURE,
        details={
            "domain": domain_name,
            "reason": "capture_filter_rejected",
            "operation": operation,
            "error": str(reply.error_class),
        },
    )


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
    "REMOTE_PCAP_DAC_REMEDIATION",
    "REMOTE_PCAP_VHOST_REMEDIATION",
    "REMOTE_PCAP_WRITE_REMEDIATION",
    "capture_volume_xml",
    "discover_dac_identity",
    "discover_netdev_id",
]
