"""Configured AF_UNIX authority client with one bounded TLS request (ADR-0611)."""

from __future__ import annotations

import asyncio
import math
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

import kdive.config as config_registry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.protocol import (
    MAX_ENVELOPE_BYTES,
    authority_server_name,
    read_frame,
)
from kdive.providers.external_boot_authority.settings import (
    WORKER_AUTHORITY_CLIENT_CERT_REF,
    WORKER_AUTHORITY_CLIENT_KEY_REF,
    WORKER_AUTHORITY_INSTANCE,
    WORKER_AUTHORITY_REQUEST_SOCKET,
    WORKER_AUTHORITY_SERVER_CA_REF,
)

_UNIX_PATH_MAX_BYTES = 107


@dataclass(frozen=True, slots=True)
class LocalAuthorityBinding:
    """The worker-owned, fixed local authority endpoint and TLS references."""

    authority_instance: str
    request_socket: Path
    server_ca_ref: str
    client_cert_ref: str
    client_key_ref: str


def local_authority_binding() -> LocalAuthorityBinding | None:
    """Return the complete configured worker route, or no route when entirely unset."""
    try:
        values = (
            config_registry.get(WORKER_AUTHORITY_INSTANCE),
            config_registry.get(WORKER_AUTHORITY_REQUEST_SOCKET),
            config_registry.get(WORKER_AUTHORITY_SERVER_CA_REF),
            config_registry.get(WORKER_AUTHORITY_CLIENT_CERT_REF),
            config_registry.get(WORKER_AUTHORITY_CLIENT_KEY_REF),
        )
    except CategorizedError:
        raise CategorizedError(
            "authority: invalid-binding", category=ErrorCategory.CONFIGURATION_ERROR
        ) from None
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise CategorizedError(
            "authority: incomplete-local-binding", category=ErrorCategory.CONFIGURATION_ERROR
        )
    instance, request_socket, server_ca_ref, client_cert_ref, client_key_ref = values
    assert isinstance(instance, str)
    assert isinstance(request_socket, Path)
    assert isinstance(server_ca_ref, str)
    assert isinstance(client_cert_ref, str)
    assert isinstance(client_key_ref, str)
    return LocalAuthorityBinding(
        authority_instance=instance,
        request_socket=request_socket,
        server_ca_ref=server_ca_ref,
        client_cert_ref=client_cert_ref,
        client_key_ref=client_key_ref,
    )


class _AuthorityUnixTransport:
    """Only the owning typed sender may submit an encoded frame to this route."""

    __slots__ = ("_request_socket", "_server_name", "_tls_material")

    def __init__(self, binding: LocalAuthorityBinding, tls_material: ssl.SSLContext) -> None:
        raw_socket = os.fspath(binding.request_socket)
        try:
            encoded = os.fsencode(raw_socket)
            if (
                not binding.authority_instance.strip()
                or "\0" in raw_socket
                or not binding.request_socket.is_absolute()
                or len(encoded) > _UNIX_PATH_MAX_BYTES
            ):
                raise ValueError
        except TypeError, ValueError:
            raise CategorizedError(
                "authority: invalid-binding", category=ErrorCategory.CONFIGURATION_ERROR
            ) from None
        self._request_socket = raw_socket
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
                    reader, writer = await asyncio.open_unix_connection(
                        self._request_socket,
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
