"""Bounded mutual-TLS Unix transport for the external-boot authority (ADR-0584)."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import socket
import ssl
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import SecretStr

from kdive.providers.external_boot_authority.protocol import (
    MAX_MESSAGE_BYTES,
    AuthorityAcknowledgementV1,
    AuthorityHealthAcknowledgementV1,
    AuthorityHealthRequestV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
    decode_authority_request,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
)

if TYPE_CHECKING:
    from kdive.providers.external_boot_authority.host import AuthorityHostConfig

MAX_ENVELOPE_BYTES = MAX_MESSAGE_BYTES
MAX_CREDENTIAL_BYTES = 4_096
SOCKET_MODE = 0o660
SOCKET_DIRECTORY_MODE = 0o2750
_TLS_TIMEOUT_SECONDS = 5.0
_MAX_JSON_NESTING = 64
_POSIX_ACL_XATTRS = frozenset({"system.posix_acl_access", "system.posix_acl_default"})

type Operation = Literal["acknowledge-takeover", "execute-mutation", "health"]
type AuthenticatePeer = Callable[[SecretStr], Awaitable[AuthenticatedPeer]]


class AuthorityService(Protocol):
    async def acknowledge_takeover(
        self, peer: AuthenticatedPeer, request: AuthorityTakeoverRequestV1
    ) -> AuthorityAcknowledgementV1: ...

    async def execute_mutation(
        self, peer: AuthenticatedPeer, request: AuthorityMutationRequestV1
    ) -> AuthorityObservationV1: ...


class _TransportError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class SocketLockBusyError(OSError):
    """The fixed authority-socket probe lock is already held."""


def authority_server_name(authority_instance: str) -> str:
    """Derive the stable reserved DNS name bound into an authority server certificate."""
    digest = hashlib.sha256(authority_instance.encode("utf-8")).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"{encoded}.authority.kdive.invalid"


def _canonical_json(value: object) -> bytes:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if not encoded or len(encoded) > MAX_ENVELOPE_BYTES:
        raise ValueError("invalid-request")
    return encoded


def encode_request_envelope(
    operation: Operation, request: dict[str, Any], credential: str
) -> bytes:
    """Encode one closed canonical request envelope for tests and future trusted clients."""
    credential_bytes = credential.encode("utf-8")
    if not credential_bytes or len(credential_bytes) > MAX_CREDENTIAL_BYTES:
        raise ValueError("credential must contain 1 through 4096 UTF-8 bytes")
    request_bytes = _canonical_json(request)
    decoded = decode_authority_request(request_bytes)
    if operation == "acknowledge-takeover" and not isinstance(decoded, AuthorityTakeoverRequestV1):
        raise ValueError("invalid-request")
    if operation == "execute-mutation" and not isinstance(decoded, AuthorityMutationRequestV1):
        raise ValueError("invalid-request")
    if operation == "health" and not isinstance(decoded, AuthorityHealthRequestV1):
        raise ValueError("invalid-request")
    return _canonical_json({"credential": credential, "operation": operation, "request": request})


async def read_frame(reader: asyncio.StreamReader, *, maximum: int) -> bytes:
    """Read one network-order frame after rejecting its bound before allocation."""
    size = int.from_bytes(await reader.readexactly(4), "big")
    if size < 1 or size > maximum:
        raise ValueError("invalid-request")
    return await reader.readexactly(size)


async def _write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if not payload or len(payload) > MAX_ENVELOPE_BYTES:
        raise ValueError("invalid-request")
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


def _reject_excessive_json_nesting(payload: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in payload:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
            continue
        if byte == ord('"'):
            in_string = True
        elif byte in (ord("["), ord("{")):
            depth += 1
            if depth > _MAX_JSON_NESTING:
                raise _TransportError("invalid-request")
        elif byte in (ord("]"), ord("}")):
            depth -= 1


def _decode_envelope(payload: bytes) -> tuple[Operation, object, SecretStr]:
    try:
        _reject_excessive_json_nesting(payload)
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {"credential", "operation", "request"}:
            raise ValueError
        if _canonical_json(value) != payload:
            raise ValueError
        operation = value["operation"]
        credential = value["credential"]
        request_value = value["request"]
        if operation not in {"acknowledge-takeover", "execute-mutation", "health"}:
            raise ValueError
        if not isinstance(credential, str) or not isinstance(request_value, dict):
            raise ValueError
        credential_bytes = credential.encode("utf-8")
        if not credential_bytes or len(credential_bytes) > MAX_CREDENTIAL_BYTES:
            raise ValueError
        request = decode_authority_request(_canonical_json(request_value))
        if operation == "acknowledge-takeover" and not isinstance(
            request, AuthorityTakeoverRequestV1
        ):
            raise ValueError
        if operation == "execute-mutation" and not isinstance(request, AuthorityMutationRequestV1):
            raise ValueError
        if operation == "health" and not isinstance(request, AuthorityHealthRequestV1):
            raise ValueError
    except RecursionError, TypeError, UnicodeError, ValueError, json.JSONDecodeError:
        raise _TransportError("invalid-request") from None
    return operation, request, SecretStr(credential)


def _success(
    value: AuthorityAcknowledgementV1 | AuthorityObservationV1 | AuthorityHealthAcknowledgementV1,
) -> bytes:
    return _canonical_json({"status": "ok", "value": value.model_dump(mode="json", by_alias=True)})


def _error(category: str) -> bytes:
    return _canonical_json({"category": category, "status": "error"})


def _service_category(category: str) -> str:
    return {"journal_conflict": "journal-conflict", "provider_conflict": "provider-conflict"}.get(
        category, category
    )


async def _dispatch(
    payload: bytes,
    authenticate_peer: AuthenticatePeer,
    service: AuthorityService | None,
) -> bytes:
    operation, request, credential = _decode_envelope(payload)
    try:
        peer = await authenticate_peer(credential)
    except Exception:  # noqa: BLE001 -- authentication details never cross the boundary
        return _error("unauthenticated")
    if operation == "health":
        return _success(AuthorityHealthAcknowledgementV1())
    if service is None:
        return _error("provider-not-configured")
    try:
        if operation == "acknowledge-takeover":
            if not isinstance(request, AuthorityTakeoverRequestV1):
                raise _TransportError("invalid-request")
            return _success(await service.acknowledge_takeover(peer, request))
        if not isinstance(request, AuthorityMutationRequestV1):
            raise _TransportError("invalid-request")
        return _success(await service.execute_mutation(peer, request))
    except AuthorityServiceError as exc:
        return _error(_service_category(exc.category))
    except Exception:  # noqa: BLE001 -- provider/service details never cross the boundary
        return _error("provider-conflict")


async def _handle_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    authenticate_peer: AuthenticatePeer,
    service: AuthorityService | None,
) -> None:
    try:
        async with asyncio.timeout(_TLS_TIMEOUT_SECONDS):
            payload = await read_frame(reader, maximum=MAX_ENVELOPE_BYTES)
            response = await _dispatch(payload, authenticate_peer, service)
            await _write_frame(writer, response)
    except _TransportError as exc:
        with suppress(ConnectionError, ssl.SSLError):
            await _write_frame(writer, _error(exc.category))
    except ValueError, json.JSONDecodeError:
        with suppress(ConnectionError, ssl.SSLError):
            await _write_frame(writer, _error("invalid-request"))
    except asyncio.IncompleteReadError, ConnectionError, TimeoutError, ssl.SSLError:
        pass
    finally:
        writer.close()
        with suppress(ConnectionError, ssl.SSLError, TimeoutError):
            await writer.wait_closed()


def _fingerprint(path: Path) -> tuple[int, int, int, int, str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise OSError("not regular")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65_536):
            digest.update(chunk)
        return (
            status.st_dev,
            status.st_ino,
            status.st_uid,
            stat.S_IMODE(status.st_mode),
            digest.hexdigest(),
        )
    finally:
        os.close(descriptor)


def validate_protected_parents(path: Path, owner_uid: int, *, require_root: bool = False) -> None:
    """Require an absolute, non-writable parent chain with the selected ownership."""
    if not path.is_absolute():
        raise OSError("protected path is not absolute")
    allowed_owner_uids = {0} if require_root else {0, owner_uid}
    current = Path(path.root)
    for part in path.parts[1:-1]:
        current /= part
        status = os.stat(current, follow_symlinks=False)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid not in allowed_owner_uids
            or stat.S_IMODE(status.st_mode) & 0o022
        ):
            raise OSError("protected parent chain is unsafe")


def validate_socket_parent(path: Path, owner_uid: int, group_gid: int) -> None:
    """Reject a request path whose parent chain is linked or whose leaf is unsafe."""
    validate_protected_parents(path, owner_uid)
    parent = os.stat(path.parent, follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != owner_uid
        or parent.st_gid != group_gid
        or stat.S_IMODE(parent.st_mode) != SOCKET_DIRECTORY_MODE
    ):
        raise OSError("authority socket parent is unsafe")
    if frozenset(os.listxattr(path.parent, follow_symlinks=False)) & _POSIX_ACL_XATTRS:
        raise OSError("authority socket parent ACL is unsafe")


def check_stale_socket(path: Path, owner_uid: int) -> None:
    """Remove only a conclusively stale, authority-owned socket inode."""
    try:
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != owner_uid:
        raise OSError("unsafe-socket")
    candidate = socket.socket(socket.AF_UNIX)
    try:
        try:
            candidate.connect(str(path))
        except ConnectionRefusedError:
            pass
        except OSError:
            raise OSError("socket-check-failed") from None
        else:
            raise OSError("live-socket")
    finally:
        candidate.close()
    try:
        after = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        raise OSError("replaced-socket") from None
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise OSError("replaced-socket")
    path.unlink()


def acquire_socket_lock(path: Path, owner_uid: int) -> int:
    """Acquire one nonblocking authority-owned lock descriptor."""
    validate_protected_parents(path, owner_uid)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != owner_uid
            or stat.S_IMODE(status.st_mode) != 0o600
        ):
            raise OSError("unsafe-lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SocketLockBusyError("busy") from None
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def server_tls_context(config: AuthorityHostConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(config.worker_client_ca))
    context.load_cert_chain(str(config.server_certificate), str(config.server_private_key))
    return context


def health_tls_context(config: AuthorityHostConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(config.server_ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.load_cert_chain(str(config.health_client_certificate), str(config.health_client_key))
    return context


@dataclass(slots=True)
class AuthorityListener:
    """A bound listener retaining the exact socket and loaded TLS evidence."""

    server: asyncio.Server
    socket_path: Path
    authority_uid: int
    request_gid: int
    server_name: str
    tls_context: ssl.SSLContext
    fingerprints: dict[Path, tuple[int, int, int, int, str]]
    socket_identity: tuple[int, int]
    lock_descriptor: int
    started: bool = False

    async def start_serving(self) -> None:
        await self.server.start_serving()
        self.started = True

    def validate(self) -> None:
        sockets = self.server.sockets or ()
        if (
            len(sockets) != 1
            or sockets[0].family != socket.AF_UNIX
            or self.tls_context.minimum_version is not ssl.TLSVersion.TLSv1_3
            or self.tls_context.maximum_version is not ssl.TLSVersion.TLSv1_3
            or self.tls_context.verify_mode is not ssl.CERT_REQUIRED
        ):
            raise OSError("listener evidence invalid")
        validate_socket_parent(self.socket_path, self.authority_uid, self.request_gid)
        status = os.stat(self.socket_path, follow_symlinks=False)
        if (
            not stat.S_ISSOCK(status.st_mode)
            or status.st_uid != self.authority_uid
            or status.st_gid != self.request_gid
            or stat.S_IMODE(status.st_mode) != SOCKET_MODE
            or (status.st_dev, status.st_ino) != self.socket_identity
            or self.server.is_serving() != self.started
        ):
            raise OSError("listener evidence invalid")
        if frozenset(os.listxattr(self.socket_path, follow_symlinks=False)) & _POSIX_ACL_XATTRS:
            raise OSError("listener socket ACL is unsafe")
        if any(_fingerprint(path) != expected for path, expected in self.fingerprints.items()):
            raise OSError("listener TLS evidence changed")

    async def close(self) -> None:
        try:
            self.server.close()
            await self.server.wait_closed()
            try:
                status = os.stat(self.socket_path, follow_symlinks=False)
            except FileNotFoundError:
                return
            if (
                stat.S_ISSOCK(status.st_mode)
                and (status.st_dev, status.st_ino) == self.socket_identity
            ):
                self.socket_path.unlink()
        finally:
            descriptor, self.lock_descriptor = self.lock_descriptor, -1
            if descriptor >= 0:
                os.close(descriptor)


async def serve_authority_transport(
    config: AuthorityHostConfig,
    authenticate_peer: AuthenticatePeer,
    service: AuthorityService | None = None,
) -> AuthorityListener:
    """Bind the dormant authenticated boundary without beginning to serve it."""
    validate_socket_parent(
        config.request_socket,
        config.authority_uid,
        config.authority_client_gid,
    )
    lock_path = config.request_socket.with_suffix(".lock")
    lock_descriptor = acquire_socket_lock(lock_path, config.authority_uid)
    try:
        check_stale_socket(config.request_socket, config.authority_uid)
        context = server_tls_context(config)
    except BaseException:
        os.close(lock_descriptor)
        raise

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_session(reader, writer, authenticate_peer, service)

    try:
        server = await asyncio.start_unix_server(
            handle,
            path=str(config.request_socket),
            ssl=context,
            ssl_handshake_timeout=_TLS_TIMEOUT_SECONDS,
            ssl_shutdown_timeout=_TLS_TIMEOUT_SECONDS,
            start_serving=False,
        )
    except BaseException:
        os.close(lock_descriptor)
        raise
    try:
        os.chmod(config.request_socket, SOCKET_MODE, follow_symlinks=False)
        status = os.stat(config.request_socket, follow_symlinks=False)
        fingerprints = _tls_fingerprints(config)
        return AuthorityListener(
            server,
            config.request_socket,
            config.authority_uid,
            config.authority_client_gid,
            authority_server_name(config.authority_instance),
            context,
            fingerprints,
            (status.st_dev, status.st_ino),
            lock_descriptor,
        )
    except BaseException:
        server.close()
        await server.wait_closed()
        os.close(lock_descriptor)
        raise


@dataclass(slots=True)
class AuthorityNetworkListener:
    """Exact IPv4 bind and TLS evidence for the optional listener (ADR-0606)."""

    server: asyncio.Server
    address: tuple[str, int]
    server_name: str
    tls_context: ssl.SSLContext
    fingerprints: dict[Path, tuple[int, int, int, int, str]]
    socket_descriptor: int
    started: bool = False

    async def start_serving(self) -> None:
        await self.server.start_serving()
        self.started = True

    def validate(self) -> None:
        sockets = self.server.sockets or ()
        if (
            len(sockets) != 1
            or sockets[0].family != socket.AF_INET
            or sockets[0].getsockname() != self.address
            or sockets[0].fileno() != self.socket_descriptor
            or self.server.is_serving() != self.started
            or self.tls_context.minimum_version is not ssl.TLSVersion.TLSv1_3
            or self.tls_context.maximum_version is not ssl.TLSVersion.TLSv1_3
            or self.tls_context.verify_mode is not ssl.CERT_REQUIRED
        ):
            raise OSError("listener evidence invalid")
        if any(_fingerprint(path) != expected for path, expected in self.fingerprints.items()):
            raise OSError("listener TLS evidence changed")

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


async def serve_authority_network_transport(
    config: AuthorityHostConfig,
    authenticate_peer: AuthenticatePeer,
    service: AuthorityService | None = None,
) -> AuthorityNetworkListener:
    """Bind one configured IPv4 mutual-TLS listener without beginning to serve it."""
    if config.network_address is None or config.network_port is None:
        raise ValueError("network listener is not configured")
    context = server_tls_context(config)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_session(reader, writer, authenticate_peer, service)

    server = await asyncio.start_server(
        handle,
        host=config.network_address,
        port=config.network_port,
        family=socket.AF_INET,
        ssl=context,
        ssl_handshake_timeout=_TLS_TIMEOUT_SECONDS,
        ssl_shutdown_timeout=_TLS_TIMEOUT_SECONDS,
        start_serving=False,
    )
    try:
        fingerprints = _tls_fingerprints(config)
        return AuthorityNetworkListener(
            server,
            (config.network_address, config.network_port),
            authority_server_name(config.authority_instance),
            context,
            fingerprints,
            server.sockets[0].fileno(),
        )
    except BaseException:
        server.close()
        await server.wait_closed()
        raise


def _tls_fingerprints(config: AuthorityHostConfig) -> dict[Path, tuple[int, int, int, int, str]]:
    return {
        path: _fingerprint(path)
        for path in (
            config.server_private_key,
            config.server_certificate,
            config.server_ca,
            config.worker_client_ca,
            config.health_client_certificate,
            config.health_client_key,
        )
    }
