"""Private typed harness for the Resource-bound TLS connector (ADR-0606)."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import stat
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority import network_client
from kdive.providers.external_boot_authority.network_client import (
    _AuthorityNetworkTransport,
    _resolve_tls_material,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityHealthAcknowledgementV1,
    AuthorityHealthRequestV1,
)
from kdive.providers.external_boot_authority.transport import (
    MAX_ENVELOPE_BYTES,
    authority_server_name,
    encode_request_envelope,
    read_frame,
)
from kdive.providers.remote_libvirt.config import RemoteAuthorityBinding
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend

pytestmark = pytest.mark.anyio

_RESPONSE = json.dumps(
    {"status": "ok", "value": {"schema": "external-boot-authority-health-v1"}}
).encode()


def _certificate(
    name: str,
    key: ec.EllipticCurvePrivateKey,
    *,
    issuer: x509.Certificate | None = None,
    issuer_key: ec.EllipticCurvePrivateKey | None = None,
    server: bool = False,
    valid: bool = True,
) -> x509.Certificate:
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    signing_key = issuer_key or key
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=2))
        .not_valid_after(now + timedelta(minutes=5) if valid else now - timedelta(minutes=1))
        .add_extension(x509.BasicConstraints(ca=issuer is None, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=issuer is None,
                crl_sign=issuer is None,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()),
            critical=False,
        )
    )
    if issuer is not None:
        eku = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
        builder = builder.add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
    if server:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(authority_server_name(name))]), critical=False
        )
    return builder.sign(signing_key, hashes.SHA256())


def _tls_material(
    root: Path, instance: str, *, server_valid: bool = True, trusted_client: bool = True
) -> dict[str, Path]:
    keys = {
        name: ec.generate_private_key(ec.SECP256R1())
        for name in ("ca", "foreign", "server", "client")
    }
    ca = _certificate("test CA", keys["ca"])
    foreign = _certificate("foreign CA", keys["foreign"])
    certificates = {
        "server_ca": ca,
        "foreign_ca": foreign,
        "server_certificate": _certificate(
            instance,
            keys["server"],
            issuer=ca,
            issuer_key=keys["ca"],
            server=True,
            valid=server_valid,
        ),
        "client_certificate": _certificate(
            "test client",
            keys["client"],
            issuer=ca if trusted_client else foreign,
            issuer_key=keys["ca" if trusted_client else "foreign"],
        ),
    }
    values = {
        name: certificate.public_bytes(serialization.Encoding.PEM)
        for name, certificate in certificates.items()
    }
    for name in ("server", "client"):
        values[f"{name}_key"] = keys[name].private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    paths = {}
    for name, value in values.items():
        path = root / name.replace("_", "-")
        path.write_bytes(value)
        path.chmod(0o400)
        paths[name] = path
    return paths


class _HealthHarness:
    def __init__(self, transport: _AuthorityNetworkTransport) -> None:
        self._transport = transport

    async def health(self, *, deadline: float) -> AuthorityHealthAcknowledgementV1:
        envelope = encode_request_envelope(
            "health",
            AuthorityHealthRequestV1().model_dump(mode="json", by_alias=True),
            "anonymous-test-incarnation",
        )
        response = await self._transport._request_frame(envelope, deadline=deadline)
        return AuthorityHealthAcknowledgementV1.model_validate(json.loads(response)["value"])


def _binding(port: int = 1) -> RemoteAuthorityBinding:
    return RemoteAuthorityBinding(
        authority_instance="authority-a",
        address="127.0.0.1",
        port=port,
        server_ca_ref="server-ca",
        client_cert_ref="client-certificate",
        client_key_ref="client-key",  # pragma: allowlist secret - fixture reference
    )


@pytest.fixture
def material(tmp_path: Path) -> dict[str, Path]:
    return _tls_material(tmp_path, "authority-a")


def _client(binding: RemoteAuthorityBinding, root: Path) -> _HealthHarness:
    backend = FileRefBackend(root, SecretRegistry())
    return _HealthHarness(
        _AuthorityNetworkTransport(binding, _resolve_tls_material(binding, backend))
    )


@asynccontextmanager
async def _server(
    material: dict[str, Path],
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
    *,
    tls: bool = True,
    version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_3,
) -> AsyncIterator[RemoteAuthorityBinding]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = version
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(material["server_ca"]))
    context.load_cert_chain(str(material["server_certificate"]), str(material["server_key"]))
    tasks: set[asyncio.Task[None]] = set()

    async def session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await handler(reader, writer)
        finally:
            writer.close()
            with suppress(ConnectionError, ssl.SSLError):
                await writer.wait_closed()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks.add(asyncio.create_task(session(reader, writer)))

    async with await asyncio.start_server(
        connected, "127.0.0.1", 0, family=socket.AF_INET, ssl=context if tls else None
    ) as server:
        try:
            yield _binding(server.sockets[0].getsockname()[1])
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _healthy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    request = json.loads(await read_frame(reader, maximum=MAX_ENVELOPE_BYTES))
    assert request["operation"] == "health"
    assert request["credential"] == "anonymous-test-incarnation"
    assert writer.get_extra_info("ssl_object").version() == "TLSv1.3"
    writer.write(len(_RESPONSE).to_bytes(4, "big") + _RESPONSE)
    await writer.drain()


async def test_fixed_ipv4_destination_needs_no_dns(
    tmp_path: Path, material: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_dns(*args: object, **kwargs: object) -> None:
        pytest.fail("numeric authority destination must not resolve DNS")

    async with _server(material, _healthy) as binding:
        monkeypatch.setattr(socket, "getaddrinfo", no_dns)
        result = await _client(binding, tmp_path).health(
            deadline=asyncio.get_running_loop().time() + 2
        )
        assert result == AuthorityHealthAcknowledgementV1()


@pytest.mark.parametrize(
    "address", ["::1", "::ffff:127.0.0.1", "authority.invalid", "0.0.0.0", "224.0.0.1"]
)
def test_invalid_destination_rejected(address: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        _AuthorityNetworkTransport(
            replace(_binding(), address=address), ssl.create_default_context()
        )
    assert caught.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert address not in str(caught.value)


@pytest.mark.parametrize("port", [0, 65536, True])
def test_invalid_port_rejected(port: int) -> None:
    with pytest.raises(CategorizedError) as caught:
        _AuthorityNetworkTransport(replace(_binding(), port=port), ssl.create_default_context())
    assert caught.value.category == ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize("fault", ["name", "expired", "untrusted", "client", "tls12"])
async def test_tls_rejections_are_redacted(tmp_path: Path, fault: str) -> None:
    material = _tls_material(
        tmp_path, "authority-a", server_valid=fault != "expired", trusted_client=fault != "client"
    )
    version = ssl.TLSVersion.TLSv1_2 if fault == "tls12" else ssl.TLSVersion.TLSv1_3
    async with _server(material, _healthy, version=version) as binding:
        if fault == "name":
            binding = replace(binding, authority_instance="authority-b")
        if fault == "untrusted":
            binding = replace(binding, server_ca_ref="foreign-ca")
        with pytest.raises(CategorizedError) as caught:
            await _client(binding, tmp_path).health(deadline=asyncio.get_running_loop().time() + 2)
        assert caught.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
        diagnostic = "".join(traceback.format_exception(caught.value))
        assert "127.0.0.1" not in diagnostic
        assert authority_server_name(binding.authority_instance) not in diagnostic
        assert "CERTIFICATE_VERIFY_FAILED" not in diagnostic


@pytest.mark.parametrize("tls", [False, True])
async def test_stalled_handshake_or_response_is_bounded(
    tmp_path: Path, material: dict[str, Path], tls: bool
) -> None:
    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.Event().wait()

    async with _server(material, stall, tls=tls) as binding:
        client = _client(binding, tmp_path)
        with pytest.raises(CategorizedError, match="deadline-exceeded"):
            async with asyncio.timeout(2):
                await client.health(deadline=asyncio.get_running_loop().time() + 0.05)


@pytest.mark.parametrize(
    "frame", [b"\0\0\0\0", (MAX_ENVELOPE_BYTES + 1).to_bytes(4, "big"), b"\0\0\0\x08x"]
)
async def test_malformed_response_frame_is_bounded(
    tmp_path: Path, material: dict[str, Path], frame: bytes
) -> None:
    async def malformed(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_frame(reader, maximum=MAX_ENVELOPE_BYTES)
        writer.write(frame)
        await writer.drain()

    async with _server(material, malformed) as binding:
        with pytest.raises(CategorizedError, match="invalid-response"):
            await _client(binding, tmp_path).health(deadline=asyncio.get_running_loop().time() + 2)


def test_tls_files_are_private_registered_then_removed(
    tmp_path: Path, material: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SecretRegistry()
    loaded: list[Path] = []
    original = ssl.SSLContext.load_cert_chain

    def inspect(context: ssl.SSLContext, certfile: str, keyfile: str) -> None:
        for path in (Path(certfile), Path(keyfile)):
            loaded.append(path)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert path.read_text().rstrip("\n") in registry.snapshot()
        original(context, certfile, keyfile)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", inspect)
    context = _resolve_tls_material(_binding(), FileRefBackend(tmp_path, registry))
    assert context.minimum_version == context.maximum_version == ssl.TLSVersion.TLSv1_3
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert len(loaded) == 2
    assert all(not path.parent.exists() for path in loaded)


@pytest.mark.parametrize("fault", ["missing", "malformed"])
def test_tls_material_errors_redact_refs_and_cleanup(
    tmp_path: Path, material: dict[str, Path], monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    monkeypatch.setattr(network_client.tempfile, "tempdir", str(tmp_path))
    binding = (
        # pragma: allowlist nextline secret - deliberately missing fixture reference
        replace(_binding(), client_key_ref="private-missing-ref")
        if fault == "missing"
        else _binding()
    )
    if fault == "malformed":
        material["client_key"].chmod(0o600)
        material["client_key"].write_text("private-invalid-key-material")
    before = set(tmp_path.iterdir())
    with pytest.raises(CategorizedError) as caught:
        _resolve_tls_material(binding, FileRefBackend(tmp_path, SecretRegistry()))
    assert caught.value.category == ErrorCategory.CONFIGURATION_ERROR
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert "private-missing-ref" not in diagnostic
    assert "private-invalid-key-material" not in diagnostic
    assert set(tmp_path.iterdir()) == before


class _Writer:
    def __init__(self, blocked: str, advance: Callable[[], None]) -> None:
        self.blocked = blocked
        self.advance = advance
        self.closed = False
        self.aborted = False
        self.transport = self

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        self.advance()
        if self.blocked == "write":
            await asyncio.Event().wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.advance()
        if self.blocked == "close":
            await asyncio.Event().wait()

    def abort(self) -> None:
        self.aborted = True


@pytest.mark.parametrize("blocked", ["connect", "write", "read", "close", "cancel"])
async def test_one_deadline_and_cleanup_across_io(
    tmp_path: Path, material: dict[str, Path], monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    entered = asyncio.Event()
    deadlines: list[float | None] = []
    original_timeout_at = asyncio.timeout_at

    def timeout_at(when: float | None) -> asyncio.Timeout:
        deadlines.append(when)
        return original_timeout_at(when)

    def advance() -> None:
        entered.set()

    writer = _Writer(blocked, advance)

    async def connect(*args: object, **kwargs: object) -> tuple[asyncio.StreamReader, Any]:
        if blocked == "connect":
            await asyncio.Event().wait()
        reader = asyncio.StreamReader()
        if blocked not in {"read", "cancel"}:
            reader.feed_data(len(_RESPONSE).to_bytes(4, "big") + _RESPONSE)
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(asyncio, "timeout_at", timeout_at)
    deadline = asyncio.get_running_loop().time() + 0.1
    client = _client(_binding(), tmp_path)
    task = asyncio.create_task(client.health(deadline=deadline))
    if blocked == "cancel":
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CategorizedError, match="deadline-exceeded"):
            async with original_timeout_at(deadline + 2):
                await task
    assert deadlines and all(value == deadline for value in deadlines)
    if blocked != "connect":
        assert writer.closed
        assert writer.aborted


@pytest.mark.parametrize("deadline", [0.0, -1.0, float("inf"), float("nan")])
async def test_invalid_deadline_never_connects(
    tmp_path: Path, material: dict[str, Path], monkeypatch: pytest.MonkeyPatch, deadline: float
) -> None:
    async def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid deadline must fail before opening a socket")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    with pytest.raises(CategorizedError):
        await _client(_binding(), tmp_path).health(deadline=deadline)
