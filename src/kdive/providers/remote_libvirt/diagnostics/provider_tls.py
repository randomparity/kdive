"""Direct-TLS provider_tls probe for the remote-libvirt worker-vantage check (ADR-0164).

A failed libvirt qemu+tls *open* is wrapped opaquely as ``TRANSPORT_FAILURE`` by the transport, so
a bad cert is indistinguishable from a down host there — the exact distinction this check exists to
make. The probe instead does a direct TLS handshake (Python ``ssl``) to the libvirt TLS endpoint
(host/port parsed from the URI, default 16514) with the materialized client cert/key and the
configured CA, classifying via typed ``ssl`` exceptions so the verdict is stable across libvirt
versions. Scoped to chain validity, not libvirt's ``tls_allowed_dn_list`` authz.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from kdive.diagnostics.checks import TlsProbe, TlsProbeOutcome
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
from kdive.providers.remote_libvirt.connection.transport import materialized_pkipath
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_DEFAULT_TLS_PORT = 16514
_CONNECT_TIMEOUT_S = 5.0
# libvirt's materialized pkipath filenames (kdive.providers.remote_libvirt.connection.transport).
_CA_CERT_NAME = "cacert.pem"
_CLIENT_CERT_NAME = "clientcert.pem"
_CLIENT_KEY_NAME = "clientkey.pem"  # pragma: allowlist secret - libvirt file name, not a value
_log = logging.getLogger(__name__)

TlsConnector = Callable[[str, int, ssl.SSLContext], None]
ContextFactory = Callable[[RemoteLibvirtConfig], ssl.SSLContext]


def tls_endpoint(uri: str) -> tuple[str, int]:
    """Parse the libvirt TLS ``(host, port)`` from the qemu+tls URI; default 16514 when absent."""
    parsed = urlsplit(uri)
    return parsed.hostname or "", parsed.port or _DEFAULT_TLS_PORT


def _default_secret_backend() -> SecretBackend:
    # A fresh per-probe registry: short-lived and read-only (mirrors the reachability probe).
    return secret_backend_from_env(registry=SecretRegistry())


def _handshake(host: str, port: int, context: ssl.SSLContext) -> None:
    with (
        socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_S) as sock,
        context.wrap_socket(sock, server_hostname=host) as tls,
    ):
        tls.do_handshake()


def _context_factory(config: RemoteLibvirtConfig) -> ssl.SSLContext:
    backend = _default_secret_backend()
    with materialized_pkipath(backend, config.cert_refs) as pkipath:
        directory = Path(pkipath)
        ctx = ssl.create_default_context(cafile=str(directory / _CA_CERT_NAME))
        ctx.load_cert_chain(
            certfile=str(directory / _CLIENT_CERT_NAME),
            keyfile=str(directory / _CLIENT_KEY_NAME),
        )
        return ctx


def provider_tls_probe(
    config: RemoteLibvirtConfig,
    *,
    connector: TlsConnector = _handshake,
    context_factory: ContextFactory = _context_factory,
) -> TlsProbe:
    """Build the async provider_tls probe over injectable TLS seams."""
    host, port = tls_endpoint(config.uri)

    async def probe(_ca_path: str) -> TlsProbeOutcome:
        return await asyncio.to_thread(_probe_sync, host, port, config, connector, context_factory)

    return probe


def _probe_sync(
    host: str,
    port: int,
    config: RemoteLibvirtConfig,
    connector: TlsConnector,
    context_factory: ContextFactory,
) -> TlsProbeOutcome:
    try:
        context = context_factory(config)
        connector(host, port, context)
    except ssl.SSLCertVerificationError:
        return TlsProbeOutcome.INVALID
    except ConnectionRefusedError, TimeoutError, OSError, ssl.SSLError:
        _log.warning("provider_tls handshake to %s:%s did not validate", host, port, exc_info=True)
        return TlsProbeOutcome.UNREACHABLE
    return TlsProbeOutcome.VALID
