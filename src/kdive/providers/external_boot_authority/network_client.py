"""Private Resource-bound authority connector with one IO deadline (ADR-0606)."""

from __future__ import annotations

import asyncio
import math
import os
import socket
import ssl
import tempfile
from ipaddress import IPv4Address
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.transport import (
    MAX_ENVELOPE_BYTES,
    authority_server_name,
    read_frame,
)
from kdive.providers.remote_libvirt.config import RemoteAuthorityBinding
from kdive.security.secrets.secrets import SecretBackend


def _resolve_tls_material(
    binding: RemoteAuthorityBinding, secret_backend: SecretBackend
) -> ssl.SSLContext:
    """Load registered secret material into TLS; remove private files before returning."""
    try:
        certificate = secret_backend.resolve(binding.client_cert_ref)
        key = secret_backend.resolve(binding.client_key_ref)
        ca = secret_backend.resolve(binding.server_ca_ref)
    except CategorizedError as exc:
        raise CategorizedError("authority: tls-secret-unavailable", category=exc.category) from None
    except OSError, ValueError:
        raise CategorizedError(
            "authority: tls-secret-unavailable", category=ErrorCategory.CONFIGURATION_ERROR
        ) from None
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.load_verify_locations(cadata=ca)
        # Match the remote-libvirt pkipath ownership pattern, without diagnostics
        # containing secret refs, filesystem paths, or OpenSSL error text.
        with tempfile.TemporaryDirectory(prefix="kdive-authority-pki-") as directory:
            certfile = Path(directory) / "certificate.pem"
            keyfile = Path(directory) / "key.pem"
            for path, value in ((certfile, certificate), (keyfile, key)):
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(value)
            context.load_cert_chain(str(certfile), str(keyfile))
        return context
    except OSError, ValueError:
        raise CategorizedError(
            "authority: tls-material-invalid", category=ErrorCategory.CONFIGURATION_ERROR
        ) from None


class _AuthorityNetworkTransport:
    """Only the owning typed sender may submit an encoded frame to this route."""

    __slots__ = ("_address", "_port", "_server_name", "_tls_material")

    def __init__(self, binding: RemoteAuthorityBinding, tls_material: ssl.SSLContext) -> None:
        try:
            address = IPv4Address(binding.address)
            if (
                str(address) != binding.address
                or address.is_unspecified
                or address.is_multicast
                or type(binding.port) is not int
                or not 1 <= binding.port <= 65535
                or not binding.authority_instance.strip()
            ):
                raise ValueError
        except ValueError:
            raise CategorizedError(
                "authority: invalid-binding", category=ErrorCategory.CONFIGURATION_ERROR
            ) from None
        self._address = binding.address
        self._port = binding.port
        self._server_name = authority_server_name(binding.authority_instance)
        self._tls_material = tls_material

    async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
        """Spend one absolute event-loop monotonic deadline, including TLS shutdown."""
        remaining = deadline - asyncio.get_running_loop().time()
        if not math.isfinite(deadline) or deadline <= 0 or remaining <= 0:
            raise CategorizedError(
                "authority: deadline-exceeded", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        if not envelope or len(envelope) > MAX_ENVELOPE_BYTES:
            raise CategorizedError(
                "authority: invalid-request", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        writer: asyncio.StreamWriter | None = None
        received = False
        try:
            async with asyncio.timeout_at(deadline):
                try:
                    reader, writer = await asyncio.open_connection(
                        self._address,
                        self._port,
                        family=socket.AF_INET,
                        ssl=self._tls_material,
                        server_hostname=self._server_name,
                        ssl_handshake_timeout=remaining,
                        ssl_shutdown_timeout=remaining,
                        limit=MAX_ENVELOPE_BYTES,
                    )
                    writer.write(len(envelope).to_bytes(4, "big") + envelope)
                    await writer.drain()
                    response = await read_frame(reader, maximum=MAX_ENVELOPE_BYTES)
                    received = True
                finally:
                    if writer is not None:
                        writer.close()
                        if not received:
                            writer.transport.abort()
                        else:
                            try:
                                await writer.wait_closed()
                            except BaseException:
                                writer.transport.abort()
                                raise
            return response
        except TimeoutError:
            reason = "deadline-exceeded"
        except ssl.SSLError:
            reason = "tls-rejected"
        except asyncio.IncompleteReadError, ValueError:
            reason = "invalid-response"
        except OSError:
            reason = "transport-failed"
        raise CategorizedError(
            f"authority: {reason}", category=ErrorCategory.INFRASTRUCTURE_FAILURE
        ) from None
