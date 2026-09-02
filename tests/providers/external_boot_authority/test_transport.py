"""Authenticated dormant Unix transport for the external-boot authority."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import ssl
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import SecretStr

from kdive.providers.external_boot_authority import transport
from kdive.providers.external_boot_authority.host import (
    AuthorityHostConfig,
    HostReadinessError,
    check_tls_health,
)
from kdive.providers.external_boot_authority.service import AuthenticatedPeer
from kdive.providers.external_boot_authority.transport import (
    MAX_CREDENTIAL_BYTES,
    MAX_ENVELOPE_BYTES,
    authority_server_name,
    encode_request_envelope,
    read_frame,
    serve_authority_transport,
)


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Keep trust-path tests below an owner-controlled, non-writable parent chain."""
    with tempfile.TemporaryDirectory(prefix="kdive-authority-test-", dir=Path.home()) as path:
        yield Path(path)


def _write(path: Path, content: bytes, mode: int) -> Path:
    path.write_bytes(content)
    path.chmod(mode)
    return path


def _certificate(
    *,
    subject: str,
    issuer: x509.Name,
    issuer_key: ec.EllipticCurvePrivateKey,
    public_key: ec.EllipticCurvePublicKey,
    eku: x509.ObjectIdentifier,
    dns_name: str | None = None,
    valid: bool = True,
    valid_seconds: float = 300,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1) if valid else now - timedelta(minutes=2))
        .not_valid_after(
            now + timedelta(seconds=valid_seconds) if valid else now - timedelta(minutes=1)
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
    )
    if dns_name is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _tls_material(
    root: Path,
    instance: str,
    *,
    client_eku: x509.ObjectIdentifier = ExtendedKeyUsageOID.CLIENT_AUTH,
    server_eku: x509.ObjectIdentifier = ExtendedKeyUsageOID.SERVER_AUTH,
    server_name: str | None = None,
    trusted_client: bool = True,
    server_valid: bool = True,
    client_valid: bool = True,
    ca_valid: bool = True,
    client_valid_seconds: float = 300,
) -> dict[str, Path]:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "authority test CA")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5) if ca_valid else now - timedelta(minutes=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    foreign_key = ec.generate_private_key(ec.SECP256R1())
    foreign_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "foreign CA")])
    foreign_ca = (
        x509.CertificateBuilder()
        .subject_name(foreign_name)
        .issuer_name(foreign_name)
        .public_key(foreign_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(foreign_key.public_key()), critical=False
        )
        .sign(foreign_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = _certificate(
        subject="authority server",
        issuer=ca_name,
        issuer_key=ca_key,
        public_key=server_key.public_key(),
        eku=server_eku,
        dns_name=server_name or authority_server_name(instance),
        valid=server_valid,
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client_ca_name = ca_name if trusted_client else foreign_name
    client_ca_key = ca_key if trusted_client else foreign_key
    client = _certificate(
        subject="authority client",
        issuer=client_ca_name,
        issuer_key=client_ca_key,
        public_key=client_key.public_key(),
        eku=client_eku,
        valid=client_valid,
        valid_seconds=client_valid_seconds,
    )

    paths = {
        "server_certificate": root / "server-certificate",
        "server_key": root / "service-credential",
        "server_ca": root / "server-ca",
        "worker_client_ca": root / "worker-client-ca",
        "health_client_certificate": root / "health-client-certificate",
        "health_client_key": root / "health-client-key",
        "client_certificate": root / "client-certificate",
        "client_key": root / "client-key",
        "foreign_ca": root / "foreign-ca",
    }
    _write(paths["server_certificate"], server.public_bytes(serialization.Encoding.PEM), 0o400)
    _write(
        paths["server_key"],
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o400,
    )
    _write(paths["server_ca"], ca.public_bytes(serialization.Encoding.PEM), 0o400)
    _write(paths["worker_client_ca"], ca.public_bytes(serialization.Encoding.PEM), 0o400)
    _write(
        paths["health_client_certificate"], client.public_bytes(serialization.Encoding.PEM), 0o400
    )
    _write(
        paths["health_client_key"],
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o400,
    )
    _write(paths["client_certificate"], client.public_bytes(serialization.Encoding.PEM), 0o444)
    _write(
        paths["client_key"],
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o400,
    )
    _write(paths["foreign_ca"], foreign_ca.public_bytes(serialization.Encoding.PEM), 0o444)
    return paths


def _config(tmp_path: Path, material: dict[str, Path], instance: str = "authority-a"):
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    database_dsn = _write(tmp_path / "database-dsn", b"postgresql:///kdive", 0o400)
    provider_socket = tmp_path / "provider.sock"
    request_dir = tmp_path / "request"
    request_dir.mkdir(mode=0o2750)
    request_dir.chmod(0o2750)
    return AuthorityHostConfig(
        authority_instance=instance,
        authority_uid=os.geteuid(),
        journal_dir=journal,
        request_socket=request_dir / "authority.sock",
        provider_socket=provider_socket,
        database_dsn=database_dsn,
        server_private_key=material["server_key"],
        server_certificate=material["server_certificate"],
        server_ca=material["server_ca"],
        worker_client_ca=material["worker_client_ca"],
        health_client_certificate=material["health_client_certificate"],
        health_client_key=material["health_client_key"],
    )


def _request() -> dict[str, Any]:
    authority_id = uuid4()
    system_id = uuid4()
    return {
        "schema": "external-boot-authority-v1",
        "authority_id": str(authority_id),
        "generation": 1,
        "system_id": str(system_id),
        "activation_id": str(uuid4()),
        "run_id": str(uuid4()),
        "plan_identity": "sha256:" + "1" * 64,
        "purpose": "activate",
        "operation": "activate",
        "provider_kind": "local-libvirt",
        "authority_instance": "authority-a",
        "operation_identity": "operation-a",
        "operation_digest": "sha256:" + "2" * 64,
    }


def _client_context(material: dict[str, Path], *, certificate: bool = True) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(material["server_ca"]))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    if certificate:
        context.load_cert_chain(str(material["client_certificate"]), str(material["client_key"]))
    return context


async def _exchange(
    config: AuthorityHostConfig,
    material: dict[str, Path],
    payload: bytes,
    *,
    context: ssl.SSLContext | None = None,
    server_name: str | None = None,
) -> bytes:
    reader, writer = await asyncio.open_unix_connection(
        str(config.request_socket),
        ssl=context or _client_context(material),
        server_hostname=server_name or authority_server_name(config.authority_instance),
    )
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()
    response = await read_frame(reader, maximum=MAX_ENVELOPE_BYTES)
    writer.close()
    await writer.wait_closed()
    return response


def test_transport_requires_mutual_tls(tmp_path: Path) -> None:
    async def run() -> None:
        material = _tls_material(tmp_path, "authority-a")
        config = _config(tmp_path, material)

        async def authenticate(_credential: SecretStr) -> AuthenticatedPeer:
            pytest.fail("TLS rejection must happen before worker authentication")

        listener = await serve_authority_transport(config, authenticate)
        await listener.start_serving()
        try:
            with pytest.raises((ConnectionError, ssl.SSLError, asyncio.IncompleteReadError)):
                await _exchange(
                    config,
                    material,
                    encode_request_envelope("acknowledge-takeover", _request(), "credential"),
                    context=_client_context(material, certificate=False),
                )
        finally:
            await listener.close()

    asyncio.run(run())


def test_transport_rejects_oversize_before_read() -> None:
    assert MAX_ENVELOPE_BYTES == 1_048_576
    assert MAX_CREDENTIAL_BYTES == 4_096

    async def run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data((MAX_ENVELOPE_BYTES + 1).to_bytes(4, "big"))
        with pytest.raises(ValueError, match="invalid-request"):
            await read_frame(reader, maximum=MAX_ENVELOPE_BYTES)

    asyncio.run(run())


def test_transport_authenticates_incarnation_before_dispatch(tmp_path: Path) -> None:
    async def run() -> None:
        material = _tls_material(tmp_path, "authority-a")
        config = _config(tmp_path, material)
        seen: list[str] = []

        async def reject(credential: SecretStr) -> AuthenticatedPeer:
            seen.append(credential.get_secret_value())
            raise ValueError("credential rejected")

        class Service:
            async def acknowledge_takeover(self, *_args: object) -> object:
                pytest.fail("invalid worker credential reached service dispatch")

        listener = await serve_authority_transport(config, reject, service=cast(Any, Service()))
        await listener.start_serving()
        try:
            encoded = encode_request_envelope(
                "acknowledge-takeover", _request(), "sensitive-worker-credential"
            )
            response = json.loads(await _exchange(config, material, encoded))
            assert response == {"category": "unauthenticated", "status": "error"}
            assert seen == ["sensitive-worker-credential"]
            assert "sensitive-worker-credential" not in json.dumps(response)
            with pytest.raises(ValueError, match="4096"):
                encode_request_envelope(
                    "acknowledge-takeover", _request(), "x" * (MAX_CREDENTIAL_BYTES + 1)
                )
        finally:
            await listener.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-instance",
        "wrong-server-eku",
        "wrong-client-eku",
        "expired-server",
        "expired-client",
        "expired-ca",
        "untrusted",
    ],
)
def test_transport_binds_certificate_purpose_and_instance(tmp_path: Path, fault: str) -> None:
    async def run() -> None:
        material = _tls_material(
            tmp_path,
            "authority-a",
            server_name=authority_server_name("authority-b") if fault == "wrong-instance" else None,
            server_eku=(
                ExtendedKeyUsageOID.CLIENT_AUTH
                if fault == "wrong-server-eku"
                else ExtendedKeyUsageOID.SERVER_AUTH
            ),
            client_eku=(
                ExtendedKeyUsageOID.SERVER_AUTH
                if fault == "wrong-client-eku"
                else ExtendedKeyUsageOID.CLIENT_AUTH
            ),
            trusted_client=fault != "untrusted",
            server_valid=fault != "expired-server",
            client_valid=fault != "expired-client",
            ca_valid=fault != "expired-ca",
        )
        config = _config(tmp_path, material)
        called = False

        async def authenticate(_credential: SecretStr) -> AuthenticatedPeer:
            nonlocal called
            called = True
            return AuthenticatedPeer("worker")

        listener = await serve_authority_transport(config, authenticate)
        await listener.start_serving()
        try:
            with pytest.raises((ConnectionError, ssl.SSLError, asyncio.IncompleteReadError)):
                await _exchange(
                    config,
                    material,
                    encode_request_envelope("acknowledge-takeover", _request(), "credential"),
                )
            assert called is False
        finally:
            await listener.close()

    asyncio.run(run())


def test_dormant_transport_refuses_before_provider_dispatch(tmp_path: Path) -> None:
    async def run() -> None:
        material = _tls_material(tmp_path, "authority-a")
        config = _config(tmp_path, material)

        async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
            assert credential.get_secret_value() == "credential"
            return AuthenticatedPeer("worker-a")

        listener = await serve_authority_transport(config, authenticate, service=None)
        await listener.start_serving()
        try:
            response = json.loads(
                await _exchange(
                    config,
                    material,
                    encode_request_envelope("acknowledge-takeover", _request(), "credential"),
                )
            )
            assert response == {"category": "provider-not-configured", "status": "error"}
        finally:
            await listener.close()

    asyncio.run(run())


def test_transport_recovers_stale_main_socket_and_rejects_unsafe_owners(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run() -> None:
        material = _tls_material(tmp_path, "authority-a")
        config = _config(tmp_path, material)

        async def authenticate(_credential: SecretStr) -> AuthenticatedPeer:
            return AuthenticatedPeer("worker-a")

        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(config.request_socket))
        stale.close()
        listener = await serve_authority_transport(config, authenticate)
        with pytest.raises(OSError, match="busy"):
            await serve_authority_transport(config, authenticate)
        await listener.close()
        assert not config.request_socket.exists()

        live = socket.socket(socket.AF_UNIX)
        live.bind(str(config.request_socket))
        live.listen()
        try:
            with pytest.raises(OSError, match="live-socket"):
                await serve_authority_transport(config, authenticate)
        finally:
            live.close()
            config.request_socket.unlink()

        foreign = socket.socket(socket.AF_UNIX)
        foreign.bind(str(config.request_socket))
        foreign.close()
        real_stat = transport.os.stat

        def foreign_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            status = real_stat(path, *args, **kwargs)
            if Path(path) == config.request_socket:
                return type(status)(
                    (
                        status.st_mode,
                        status.st_ino,
                        status.st_dev,
                        status.st_nlink,
                        status.st_uid + 1,
                        status.st_gid,
                        status.st_size,
                        status.st_atime,
                        status.st_mtime,
                        status.st_ctime,
                    )
                )
            return status

        monkeypatch.setattr(transport.os, "stat", foreign_stat)
        with pytest.raises(OSError, match="unsafe-socket"):
            await serve_authority_transport(config, authenticate)
        monkeypatch.setattr(transport.os, "stat", real_stat)
        config.request_socket.unlink()

        target = tmp_path / "target"
        target.touch()
        config.request_socket.symlink_to(target)
        with pytest.raises(OSError, match="unsafe-socket"):
            await serve_authority_transport(config, authenticate)

    asyncio.run(run())


def test_transport_rejects_linked_request_parent(tmp_path: Path) -> None:
    async def run() -> None:
        material = _tls_material(tmp_path, "authority-a")
        config = _config(tmp_path, material)
        real_parent = tmp_path / "real-request"
        real_parent.mkdir(mode=0o750)
        linked_parent = tmp_path / "linked-request"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        config = replace(config, request_socket=linked_parent / "authority.sock")

        async def authenticate(_credential: SecretStr) -> AuthenticatedPeer:
            return AuthenticatedPeer("worker-a")

        with pytest.raises(OSError, match="parent chain"):
            await serve_authority_transport(config, authenticate)

    asyncio.run(run())


def test_listener_evidence_detects_socket_and_credential_drift(tmp_path: Path) -> None:
    async def run() -> None:
        material = _tls_material(tmp_path, "authority-a")
        config = _config(tmp_path, material)

        async def authenticate(_credential: SecretStr) -> AuthenticatedPeer:
            return AuthenticatedPeer("worker-a")

        listener = await serve_authority_transport(config, authenticate)
        try:
            assert config.request_socket.stat().st_gid == config.request_socket.parent.stat().st_gid
            listener.validate()
            assert listener.server.is_serving() is False
            await listener.start_serving()
            listener.validate()
            listener.tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
            with pytest.raises(OSError, match="listener evidence invalid"):
                listener.validate()
            listener.tls_context.minimum_version = ssl.TLSVersion.TLSv1_3
            config.request_socket.chmod(0o600)
            with pytest.raises(OSError, match="listener evidence invalid"):
                listener.validate()
            config.request_socket.chmod(0o660)
            material["worker_client_ca"].chmod(0o644)
            material["worker_client_ca"].write_bytes(
                material["worker_client_ca"].read_bytes() + b"\n"
            )
            with pytest.raises(OSError, match="TLS evidence changed"):
                listener.validate()
        finally:
            await listener.close()

    asyncio.run(run())


def test_listener_rejects_valid_health_chain_credential_replacement(tmp_path: Path) -> None:
    async def run() -> None:
        material = _tls_material(tmp_path, "authority-a")
        config = _config(tmp_path, material)

        async def authenticate(_credential: SecretStr) -> AuthenticatedPeer:
            return AuthenticatedPeer("worker-a")

        listener = await serve_authority_transport(config, authenticate)
        await listener.start_serving()
        try:
            await check_tls_health(listener, config)
            for path in (
                config.server_ca,
                config.health_client_certificate,
                config.health_client_key,
            ):
                replacement = path.with_suffix(".replacement")
                replacement.write_bytes(path.read_bytes())
                replacement.chmod(0o400)
                replacement.replace(path)
            await check_tls_health(listener, config)
            with pytest.raises(OSError, match="TLS evidence changed"):
                listener.validate()
        finally:
            await listener.close()

    asyncio.run(run())


def test_tls_health_rechecks_unchanged_client_certificate_expiry(tmp_path: Path) -> None:
    async def run() -> None:
        material = _tls_material(
            tmp_path,
            "authority-a",
            client_valid_seconds=2,
        )
        config = _config(tmp_path, material)
        authenticated = False

        async def authenticate(_credential: SecretStr) -> AuthenticatedPeer:
            nonlocal authenticated
            authenticated = True
            return AuthenticatedPeer("worker-a")

        listener = await serve_authority_transport(config, authenticate)
        await listener.start_serving()
        try:
            await check_tls_health(listener, config)
            fingerprint = hashlib.sha256(config.health_client_certificate.read_bytes()).digest()
            await asyncio.sleep(2.25)
            with pytest.raises(HostReadinessError, match="tls-health: handshake-failed"):
                await check_tls_health(listener, config)
            current = hashlib.sha256(config.health_client_certificate.read_bytes()).digest()
            assert current == fingerprint
            assert authenticated is False
        finally:
            await listener.close()

    asyncio.run(run())


def test_authority_server_name_is_lowercase_unpadded_base32_sha256() -> None:
    digest = base64.b32encode(hashlib.sha256(b"Authority-A").digest()).decode().rstrip("=").lower()
    assert authority_server_name("Authority-A") == f"{digest}.authority.kdive.invalid"
    assert "=" not in authority_server_name("Authority-A")
    assert socket.AF_UNIX is not None
