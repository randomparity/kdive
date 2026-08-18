"""A bounded TCP reachability gate for the remote-libvirt reaper openers (ADR-0565).

libvirt's remote driver honours no connect-timeout URI parameter. It extracts a fixed set —
``name``, ``command``, ``socket``, ``pkipath``, ``tls_priority``, ``mode``, ``proxy`` and the rest —
and passes anything else through to the back end, so ``libvirt.open`` against a host that never
answers costs the operating system's TCP connect timeout, ~130 s on Linux. The reconciler's reapers
fan out over the whole declared fleet, one connection per host, from inside a transaction holding an
advisory lock, which is the hazard #1980 reports.

This gate opens and immediately closes a plain TCP connection to the endpoint the reaper is about to
open, under a timeout the operator sets, and refuses the host if it does not answer. libvirt is then
never called for that host, so the kernel's SYN retry budget is never entered. It is a gate on
reachability, not a timeout on the libvirt call: a host that completes the TCP handshake and then
stalls in the TLS handshake or in a wedged libvirtd's RPC is still unbounded (#1981), and what caps
the damage there is the reaping lane's own pass budget.

The gate is deliberately not a security control. It proves only that something accepted a TCP
connection; mutual TLS remains the sole control over who the reconciler is talking to. It sends no
bytes and reads no TLS material.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.settings import REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS

#: libvirt's registered port for the TLS transport, used when the URI names no port.
DEFAULT_LIBVIRT_TLS_PORT = 16514


class ProbeSocket(Protocol):
    """All the gate does with the connection it opened: close it. Nothing is sent or read."""

    def close(self) -> None: ...


#: Opens a TCP connection to ``(host, port)`` under ``timeout`` seconds, or raises. Injected so the
#: gate is testable without a live host; production binds :func:`socket.create_connection`.
type TcpConnect = Callable[[tuple[str, int], float], ProbeSocket]


def remote_endpoint(uri: str) -> tuple[str, int]:
    """The ``(host, port)`` a remote-libvirt URI resolves to.

    Callers reach this only for a URI ``validate_remote_uri`` has already fail-closed to the
    ``qemu+tls`` scheme, so an absent port means libvirt's TLS default rather than an unknown one.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the URI names no host. Probing ``localhost``
            instead would silently gate a host the operator never declared.
    """
    parsed = urlsplit(uri)
    host = (parsed.hostname or "").strip()
    if not host:
        raise CategorizedError(
            f"remote-libvirt URI {uri!r} names no host, so its reachability cannot be checked",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"uri": uri},
        )
    return host, parsed.port or DEFAULT_LIBVIRT_TLS_PORT


def reaper_connect_timeout() -> float:
    """The operator's per-host reaper connect bound, in seconds."""
    return float(config.require(REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS))


def require_reachable(uri: str, *, timeout: float, connect: TcpConnect | None = None) -> None:
    """Refuse ``uri`` unless its endpoint accepts a TCP connection within ``timeout`` seconds.

    The probe is closed immediately; nothing is sent. On a reachable host this costs one extra
    connection that libvirtd accepts and sees closed before the TLS handshake, which its log
    records — the price of not entering the kernel's SYN retry budget on an unreachable one.

    Raises:
        CategorizedError: ``TRANSPORT_FAILURE`` when the endpoint does not accept in time, matching
            the category a failed ``qemu+tls`` connect already raises, so the reaper fan-out's
            existing unreachable-host isolation logs and skips the host unchanged.
            ``CONFIGURATION_ERROR`` when the URI names no host.
    """
    host, port = remote_endpoint(uri)
    opener = connect if connect is not None else socket.create_connection
    try:
        probe = opener((host, port), timeout)
    except OSError as exc:
        # TimeoutError is an OSError subclass, so a refused connection and an unanswered SYN take
        # the same path: both mean this host cannot serve the reaper call now.
        raise CategorizedError(
            f"remote-libvirt host {host}:{port} did not accept a connection within {timeout}s",
            category=ErrorCategory.TRANSPORT_FAILURE,
            details={"uri": uri, "connect_timeout_seconds": str(timeout)},
        ) from exc
    probe.close()


__all__ = [
    "DEFAULT_LIBVIRT_TLS_PORT",
    "ProbeSocket",
    "TcpConnect",
    "reaper_connect_timeout",
    "remote_endpoint",
    "require_reachable",
]
