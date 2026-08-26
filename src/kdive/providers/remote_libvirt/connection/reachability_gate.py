"""A bounded TCP reachability gate for the remote-libvirt reaper openers (ADR-0565).

libvirt's remote driver honours no connect-timeout URI parameter. It extracts a fixed set —
``name``, ``command``, ``socket``, ``pkipath``, ``tls_priority``, ``mode``, ``proxy`` and the rest —
and passes anything else through to the back end, so ``libvirt.open`` against a host that never
answers costs the operating system's TCP connect timeout, ~130 s on Linux. The reconciler's reapers
fan out over the whole declared fleet, one connection per host, from inside a transaction holding an
advisory lock, which is the hazard #1980 reports.

This gate opens and immediately closes a plain TCP connection to the endpoint the reaper is about to
open, under a timeout the operator sets, and refuses the host if it does not answer. libvirt is then
never called for that host, so the kernel's SYN retry budget is never entered.

Two things are outside the bound and both are deliberate:

* **Name resolution.** ``socket.getaddrinfo`` takes no timeout, and no portable one exists. A
  declared host named by DNS therefore costs the resolver's own budget — glibc's default is
  ``timeout:5 attempts:2`` per nameserver — before the gate's clock is consulted at all. That
  matters most in the correlated case, where the partition that downed the host also downed the
  resolver. Declaring hosts by IP literal removes it entirely.
* **A host that accepts and then stalls**, in the TLS handshake or in a wedged libvirtd's RPC. The
  gate proves reachability, not liveness; what caps that is the reaping lane's own pass budget
  (#1981).

One operational note. The probe connects and closes before the TLS handshake, once per declared host
per reaper call, which is the shape connection-scanning detectors look for. On a fleet behind
fail2ban or an IDS the reconciler's own address needs an allowlist entry, or the detector will
eventually block the process that is trying to clean up after it.

The gate is not a security control. It proves only that something accepted a TCP connection; mutual
TLS remains the sole control over who the reconciler is talking to. It sends no bytes and reads no
TLS material.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.connection.uri_validation import validate_remote_transport
from kdive.providers.remote_libvirt.settings import REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS

#: libvirt's registered port for the TLS transport, used when the URI names no port.
DEFAULT_LIBVIRT_TLS_PORT = 16514


class ProbeSocket(Protocol):
    """All the gate does with the connection it opened: close it. Nothing is sent or read."""

    def close(self) -> None: ...


#: Opens a TCP connection to one resolved ``(address, port)`` under ``timeout`` seconds, or raises.
#: Injected so the gate is testable without a live host; production binds
#: :func:`socket.create_connection`.
type TcpConnect = Callable[[tuple[str, int], float], ProbeSocket]

#: Resolves ``(host, port)`` to the addresses to try, in preference order.
type Resolve = Callable[[str, int], list[tuple[str, int]]]


def remote_endpoint(uri: str) -> tuple[str, int]:
    """The ``(host, port)`` a remote-libvirt URI resolves to.

    Callers reach this only for a URI ``validate_remote_uri`` has already fail-closed to the
    ``qemu+tls`` scheme, so an absent port means libvirt's TLS default rather than an unknown one.
    Nothing upstream checks the *port*, though, so a malformed one is caught here rather than left
    to surface as a bare ``ValueError`` naming neither the URI nor the setting.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the URI names no host, or names a port that
            is not a usable TCP port. Probing ``localhost`` instead of refusing a host-less URI
            would silently gate a host the operator never declared.
    """
    # Re-checked here rather than trusted from the caller. `remote_connection` does run the full
    # `validate_remote_uri` first, but this module is a public seam #1947's capture reaper is told
    # to open through, and the scheme plus `no_verify` are what keep the probe destination inside
    # the operator's declared, TLS-only inventory. The transport subset rather than the whole
    # validator: the full one forbids a `pkipath` parameter, and by this point `remote_connection`
    # has composed exactly that onto the URI.
    validate_remote_transport(uri)
    parsed = urlsplit(uri)
    host = (parsed.hostname or "").strip()
    if not host:
        raise _misconfigured(uri, "names no host, so its reachability cannot be checked")
    try:
        port = parsed.port
    except ValueError as exc:
        raise _misconfigured(uri, "carries a port that is not an integer") from exc
    if port == 0:
        raise _misconfigured(uri, "carries port 0, which is not a libvirt endpoint")
    return host, port or DEFAULT_LIBVIRT_TLS_PORT


def _reportable(uri: str) -> str:
    """``uri`` reduced to scheme, host, port and path, for a message or an error detail.

    ``remote_connection`` composes ``?pkipath=<mkdtemp dir>`` onto the URI before handing it to the
    opener, and that directory holds the 0600 client key for the op. Reporting the composed spelling
    would put the path to live private-key material into a WARNING that ``_enter_host`` already logs
    with ``exc_info=True``. The netloc is rebuilt from the host and port rather than reused, so any
    userinfo an operator put in the URI goes with the query rather than surviving into the log.
    """
    parsed = urlsplit(uri)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        # This runs on the path that *reports* a malformed port, so it cannot re-raise on one.
        port = None
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _misconfigured(uri: str, problem: str) -> CategorizedError:
    reportable = _reportable(uri)
    return CategorizedError(
        f"remote-libvirt URI {reportable!r} {problem}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"uri": reportable},
    )


def reaper_connect_timeout() -> float:
    """The operator's per-host reaper connect bound, in seconds."""
    return float(config.require(REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS))


def _resolve_addresses(host: str, port: int) -> list[tuple[str, int]]:
    """Every address ``host`` resolves to, in ``getaddrinfo`` preference order, de-duplicated.

    Not covered by the gate's deadline: ``getaddrinfo`` accepts no timeout. De-duplicated because a
    host publishing both an A and an AAAA record can yield the same address twice across socket
    types, and each duplicate would spend deadline for nothing.
    """
    addresses: list[tuple[str, int]] = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = (str(info[4][0]), int(info[4][1]))
        if address not in addresses:
            addresses.append(address)
    return addresses


def require_reachable(
    uri: str,
    *,
    timeout: float,
    connect: TcpConnect | None = None,
    resolve: Resolve | None = None,
) -> None:
    """Refuse ``uri`` unless one of its addresses accepts a TCP connection within ``timeout``.

    Resolution happens once and every resolved address shares one monotonic deadline, so a
    dual-stack host costs ``timeout`` in total rather than once per address — which is what
    ``socket.create_connection`` would do, applying its ``timeout`` inside its own per-address loop.
    The probe is closed immediately; nothing is sent. On a reachable host that costs one extra
    connection libvirtd accepts and sees closed before the TLS handshake, which its log records —
    the price of not entering the kernel's SYN retry budget on an unreachable one. Name resolution
    is **not** inside the deadline; the module docstring says why.

    Raises:
        CategorizedError: ``TRANSPORT_FAILURE`` when no address accepts in time, matching the
            category a failed ``qemu+tls`` connect already raises, so the reaper fan-out's existing
            unreachable-host isolation logs and skips the host unchanged. ``CONFIGURATION_ERROR``
            when the URI names no host or an unusable port.
    """
    host, port = remote_endpoint(uri)
    opener = connect if connect is not None else socket.create_connection
    resolver = resolve if resolve is not None else _resolve_addresses
    try:
        addresses = resolver(host, port)
    except UnicodeError as exc:
        # `getaddrinfo` puts a name IDNA cannot encode — an over-long label, say — through the
        # 'idna' codec, which raises a ValueError subclass rather than an OSError. That is an
        # operator typo, not a down host, and saying so is the difference between a fix and a
        # fruitless ping.
        raise _misconfigured(uri, "names a host that is not an encodable domain name") from exc
    except OSError as exc:
        # To a reaper an unresolvable host and an unreachable one are the same outcome, so they take
        # the same category and the fan-out isolates both the same way.
        raise CategorizedError(
            f"remote-libvirt host {host} did not resolve",
            category=ErrorCategory.TRANSPORT_FAILURE,
            details={"uri": _reportable(uri)},
        ) from exc
    if not addresses:
        raise CategorizedError(
            f"remote-libvirt host {host} resolved to no address",
            category=ErrorCategory.TRANSPORT_FAILURE,
            details={"uri": _reportable(uri)},
        )
    deadline = time.monotonic() + timeout
    last_failure: OSError | None = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            probe = opener(address, remaining)
        except OSError as exc:
            # TimeoutError is an OSError subclass, so an unanswered SYN and a refused connection
            # take the same path. Try the next address: a dual-stack host whose IPv6 route is dead
            # is still reachable over IPv4. The last failure is kept because a refused connect
            # (libvirtd down on a live host), an unroutable one, and an expired deadline need three
            # different operator fixes, and reporting all three as a timeout would name the wrong
            # one.
            last_failure = exc
            continue
        probe.close()
        return
    raise CategorizedError(
        f"remote-libvirt host {host}:{port} was not reachable within {timeout}s: "
        f"{last_failure or 'the deadline expired before any address was tried'}",
        category=ErrorCategory.TRANSPORT_FAILURE,
        details={"uri": _reportable(uri), "connect_timeout_seconds": str(timeout)},
    ) from last_failure


__all__ = [
    "DEFAULT_LIBVIRT_TLS_PORT",
    "ProbeSocket",
    "Resolve",
    "TcpConnect",
    "reaper_connect_timeout",
    "remote_endpoint",
    "require_reachable",
]
