"""Local-libvirt Connect plane: single-attach gdbstub + drgn-live-over-SSH transports (ADR-0032).

`LocalLibvirtConnect` realizes the handler-facing `Connector` port: `open_transport(system,
"gdbstub")` resolves the System's gdbstub endpoint and `open_transport(system, "drgn-live")`
resolves the loopback-forwarded guest SSH endpoint; both enforce loopback-only **before any
network IO** (the ported v1 "F2" SSRF control), probe reachability over an injected seam (an RSP
framing probe for gdbstub, an SSH connect for drgn-live), and return an opaque `TransportHandle`
(an encoded `TransportHandleData`) the session row persists; `close_transport(handle)` validates
the handle and then no-ops (both are connectionless from the Connect plane's view). Each endpoint
resolver reads the port the provisioner recorded in the **live** libvirt domain XML over an
injected `connect` seam — the gdbstub `-gdb` port (ADR-0210 §1) and the SSH `hostfwd` port
(ADR-0218 §5); only the `libvirt.open`/`XMLDesc` calls and the real socket/SSH probes are
`# pragma: no cover - live_vm`, so both resolvers' lookup/parse/error branches and the
orchestration are unit-tested with fakes.

The RSP-framing codec (`rsp_frame`/`valid_rsp_frame`) and the bounded probe are ported from
v1 `transport/core/rsp_probe.py` + `bounded.py`: the probe exchanges one **read-only** `?`
halt-reason query and accepts only a complete, checksum-valid `$...#xx` frame, so a stale or
non-RSP listener is rejected rather than mistaken for a healthy stub.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Protocol

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.ports.handles import (
    SystemHandle,
    TransportHandle,
)
from kdive.providers.ports.lifecycle import (
    DebugTransportKind,
    TransportHandleData,
)
from kdive.providers.shared.debug_common.rsp import rsp_reachable
from kdive.providers.shared.libvirt_xml import (
    recorded_gdb_port_from_root,
    recorded_ssh_port_from_root,
)

_GDBSTUB: DebugTransportKind = "gdbstub"
_DRGN_LIVE: DebugTransportKind = "drgn-live"  # the agent-facing transport kind (ADR-0085)
_SSH_SCHEME = "ssh"  # the handle scheme local emits — its SSH realization (ADR-0039)
_LOOPBACK_HOST = "127.0.0.1"  # loopback-only: local transports never listen off-host (ADR-0210)

type _ResolveEndpoint = Callable[[SystemHandle], tuple[str, int]]
type _Probe = Callable[[str, int], bool]
type _SshConnect = Callable[[str, int], bool]


class _Domain(Protocol):
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802 - mirrors the libvirt binding name


class _Conn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> int: ...


type _Connect = Callable[[], _Conn]


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)


def _is_loopback_literal(host: str) -> bool:
    """True iff ``host`` is a loopback IP literal (a hostname is not — reject without DNS)."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalLibvirtConnect:
    """The realized `Connector` for local-libvirt transports: gdbstub and drgn-live.

    The agent-facing ``drgn-live`` transport (ADR-0085) is realized locally over SSH
    (ADR-0039); its handle keeps the ``ssh://`` scheme (a provider-internal realization
    detail). Both transports enforce loopback-only **before any network IO** (the ported v1
    "F2" SSRF control, ADR-0032 §5 / ADR-0039 §1) and probe reachability over an injected,
    ``live_vm``-gated seam: an RSP framing probe for gdbstub, an SSH connect for drgn-live.
    """

    def __init__(
        self,
        *,
        resolve_endpoint: _ResolveEndpoint,
        probe: _Probe,
        resolve_ssh_endpoint: _ResolveEndpoint | None = None,
        ssh_connect: _SshConnect | None = None,
    ) -> None:
        self._resolve_endpoint = resolve_endpoint
        self._probe = probe
        self._resolve_ssh_endpoint = (
            resolve_ssh_endpoint if resolve_ssh_endpoint is not None else _real_resolve_ssh_endpoint
        )
        self._ssh_connect = ssh_connect if ssh_connect is not None else _real_ssh_connect

    @classmethod
    def from_env(cls) -> LocalLibvirtConnect:
        """Build with the real resolvers + probers; opens no connection until a transport opens."""
        return cls(
            resolve_endpoint=_resolve_endpoint_via(_default_connect),
            probe=_real_probe,
            resolve_ssh_endpoint=_real_resolve_ssh_endpoint,
            ssh_connect=_real_ssh_connect,
        )

    def open_transport(self, system: SystemHandle, kind: DebugTransportKind) -> TransportHandle:
        """Open a single-attach transport (gdbstub or ssh) and return its handle.

        Resolves the System's endpoint, enforces loopback-only before any IO, and probes
        reachability. Runs no DB work — the caller owns the session row and the per-System
        lock (the probe deliberately runs lock-free, ADR-0032 §6a).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown kind, a non-loopback
                resolved host (no IO), an absent domain, or a System not provisioned with a
                gdbstub; ``DEBUG_ATTACH_FAILURE`` if the peer does not answer;
                ``TRANSPORT_FAILURE`` on a socket fault; ``INFRASTRUCTURE_FAILURE`` for a
                libvirt read fault.
        """
        if kind == _GDBSTUB:
            return self._open_gdbstub(system)
        if kind == _DRGN_LIVE:
            return self._open_ssh(system)
        raise _config_error(f"unsupported transport kind: {kind!r}")

    def _open_gdbstub(self, system: SystemHandle) -> TransportHandle:
        host, port = self._resolve_endpoint(system)
        if not _is_loopback_literal(host):
            raise _config_error("gdbstub host must be a loopback IP literal")
        try:
            reachable = self._probe(host, port)
        except OSError as exc:
            raise CategorizedError(
                "gdbstub transport socket fault",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"port": port},
            ) from exc
        if not reachable:
            raise CategorizedError(
                "gdbstub did not answer RSP framing",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"port": port},
            )
        return TransportHandle(TransportHandleData(kind=_GDBSTUB, host=host, port=port).encode())

    def _open_ssh(self, system: SystemHandle) -> TransportHandle:
        host, port = self._resolve_ssh_endpoint(system)
        if not _is_loopback_literal(host):
            raise _config_error("ssh host must be a loopback IP literal")
        try:
            reachable = self._ssh_connect(host, port)
        except OSError as exc:
            raise CategorizedError(
                "ssh transport socket fault",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"port": port},
            ) from exc
        if not reachable:
            raise CategorizedError(
                "ssh endpoint did not accept a connection",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"port": port},
            )
        return TransportHandle(TransportHandleData(kind=_SSH_SCHEME, host=host, port=port).encode())

    def close_transport(self, handle: TransportHandle) -> None:
        """Validate the handle, then no-op for these connectionless transports."""
        TransportHandleData.decode(handle)

    def recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None:
        """Return the recorded loopback SSH ``(host, port)``, or ``None`` if there is no forward.

        Reuses the drgn-live SSH endpoint resolver; a `CONFIGURATION_ERROR` (no domain, or no
        recorded SSH forward) maps to ``None``. Local-libvirt now renders the forward on every
        domain (ADR-0281), so a ready local System resolves an endpoint; ``None`` means the
        provider exposes no loopback SSH forward (a domain defined before that change). Any other
        libvirt/parse fault (`INFRASTRUCTURE_FAILURE`) propagates (ADR-0271).
        """
        try:
            return self._resolve_ssh_endpoint(system)
        except CategorizedError as exc:
            if exc.category is ErrorCategory.CONFIGURATION_ERROR:
                return None
            raise


def _default_connect() -> _Conn:  # pragma: no cover - live_vm
    """Open the host libvirt connection the resolver reads the domain XML through.

    ``virConnect`` structurally satisfies the narrow ``_Conn`` Protocol (``lookupByName``/
    ``close``); the lookup'd ``virDomain`` satisfies ``_Domain`` (``XMLDesc``).
    """
    return libvirt.open(config.require(LIBVIRT_URI))


def _resolve_endpoint_via(connect: _Connect) -> _ResolveEndpoint:
    """Build the gdbstub endpoint resolver over ``connect`` (ADR-0210 §1).

    The returned resolver reads the System's recorded gdbstub port from its **live** libvirt
    domain XML (the port provisioning recorded, the one QEMU actually listens on) and returns the
    loopback endpoint. The libvirt ``open``/``XMLDesc`` calls live behind ``connect``, so every
    branch below is exercised with a fake connection.
    """

    def resolve(system: SystemHandle) -> tuple[str, int]:
        domain_name = str(system)
        conn = connect()
        try:
            domain = conn.lookupByName(domain_name)
            xml = domain.XMLDesc(0)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                raise _config_error(
                    f"System {domain_name!r} has no running libvirt domain to attach a gdbstub to"
                ) from exc
            raise CategorizedError(
                "libvirt error reading the gdbstub domain XML",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"domain": domain_name},
            ) from exc
        finally:
            _close(conn)
        return (_LOOPBACK_HOST, _resolved_port(xml, domain_name))

    return resolve


def _resolved_port(xml: str, domain_name: str) -> int:
    """The gdbstub port the domain XML records; raise an actionable error otherwise.

    Malformed XML is an infrastructure read fault (libvirt handed back a broken document); a
    well-formed domain that records no ``-gdb`` port is a configuration error (the System was not
    provisioned with a gdbstub).
    """
    try:
        root = _safe_fromstring(xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise CategorizedError(
            "malformed libvirt domain XML reading the gdbstub port",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain_name},
        ) from exc
    port = recorded_gdb_port_from_root(root)
    if port is None:
        raise _config_error(
            f"System {domain_name!r} was not provisioned with a gdbstub; reprovision with "
            "the profile's debug.gdbstub set"
        )
    return port


def _close(conn: _Conn) -> None:
    with contextlib.suppress(libvirt.libvirtError):
        conn.close()


def _real_probe(host: str, port: int) -> bool:  # pragma: no cover - live_vm
    return rsp_reachable(host, port)


def _resolve_ssh_endpoint_via(connect: _Connect) -> _ResolveEndpoint:
    """Build the drgn-live SSH endpoint resolver over ``connect`` (ADR-0218 §5).

    The returned resolver reads the System's recorded forwarded SSH port from its **live** libvirt
    domain XML (the loopback port the provisioner's ``hostfwd`` recorded, the one QEMU actually
    forwards to the guest sshd) and returns the loopback endpoint. Mirrors
    ``_resolve_endpoint_via`` for the SSH transport; the libvirt ``open``/``XMLDesc`` calls live
    behind ``connect``, so every branch below is exercised with a fake connection.
    """

    def resolve(system: SystemHandle) -> tuple[str, int]:
        domain_name = str(system)
        conn = connect()
        try:
            domain = conn.lookupByName(domain_name)
            xml = domain.XMLDesc(0)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                raise _config_error(
                    f"System {domain_name!r} has no running libvirt domain to open a drgn-live "
                    "SSH transport to"
                ) from exc
            raise CategorizedError(
                "libvirt error reading the drgn-live SSH domain XML",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"domain": domain_name},
            ) from exc
        finally:
            _close(conn)
        return (_LOOPBACK_HOST, _resolved_ssh_port(xml, domain_name))

    return resolve


def _resolved_ssh_port(xml: str, domain_name: str) -> int:
    """The forwarded SSH port the domain XML records; raise an actionable error otherwise.

    Malformed XML is an infrastructure read fault (libvirt handed back a broken document); a
    well-formed domain that records no forwarded SSH port is a configuration error (the System was
    not provisioned for drgn-live).
    """
    try:
        root = _safe_fromstring(xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise CategorizedError(
            "malformed libvirt domain XML reading the drgn-live SSH port",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain_name},
        ) from exc
    port = recorded_ssh_port_from_root(root)
    if port is None:
        raise _config_error(
            f"System {domain_name!r} was not provisioned for drgn-live; reprovision with "
            "the profile's ssh_credential_ref set"
        )
    return port


_real_resolve_ssh_endpoint = _resolve_ssh_endpoint_via(_default_connect)


# sshd writes its ``SSH-<protoversion>-...`` identification string first on connect, before
# any auth (RFC 4253 §4.2). A read-only banner check proves a live sshd is listening on the
# forwarded port without performing the authenticated round-trip — that is introspect.run's
# helper (ADR-0219). The deadline bounds a peer that accepts the connection but never speaks.
_SSH_ID_PREFIX = b"SSH-"
_SSH_PROBE_TIMEOUT_S = 2.0


def _ssh_banner_verdict(buffer: bytes) -> bool | None:
    """Classify bytes accumulated from a peer that should be an sshd identification banner.

    Returns ``True`` once ``buffer`` begins with ``SSH-`` (a live sshd identified itself),
    ``False`` once ``buffer`` has diverged from that prefix (a listener that accepts TCP but
    does not speak SSH), or ``None`` while ``buffer`` is still a proper prefix of ``SSH-`` and
    more bytes may complete it.
    """
    if buffer.startswith(_SSH_ID_PREFIX):
        return True
    if not _SSH_ID_PREFIX.startswith(buffer):
        return False
    return None


def _real_ssh_connect(host: str, port: int) -> bool:  # pragma: no cover - live_vm
    """Connect and read the sshd identification banner; True iff the peer speaks SSH.

    sshd announces itself with an ``SSH-<protoversion>-...`` string immediately on connect
    (RFC 4253 §4.2), so a read-only banner check proves a live sshd is listening on the
    loopback-forwarded port — which in turn proves the guest NIC/DHCP is up and QEMU's
    ``hostfwd`` reaches it — without performing auth (introspect.run's helper does the
    authenticated round-trip, ADR-0219). A listener that accepts but never sends an SSH banner
    is rejected. Mirrors ``rsp_reachable`` for the SSH transport; runs only under the
    ``live_vm`` gate.
    """
    deadline = time.monotonic() + _SSH_PROBE_TIMEOUT_S
    sock = socket.create_connection((host, port), timeout=_SSH_PROBE_TIMEOUT_S)
    buffer = b""
    try:
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = sock.recv(256)
            except TimeoutError:
                continue
            if not chunk:
                break
            buffer += chunk
            verdict = _ssh_banner_verdict(buffer)
            if verdict is not None:
                return verdict
    finally:
        sock.close()
    return False


__all__ = ["LocalLibvirtConnect"]
