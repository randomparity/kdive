"""Tests for shared remote-libvirt connection wiring."""

from __future__ import annotations

from pathlib import Path

from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.connection.transport import RemoteLibvirtConnections

_CERT_REFS = TlsCertRefs(
    client_cert_ref="secret://client-cert",
    client_key_ref="secret://client-key",  # pragma: allowlist secret
    ca_cert_ref="secret://ca-cert",
)


def test_reaper_connections_materialize_tls_and_close_injected_connection(
    tmp_path: Path,
) -> None:
    config = RemoteLibvirtConfig(
        uri="qemu+tls://builder.example/system",
        cert_refs=_CERT_REFS,
        concurrent_allocation_cap=1,
    )
    conn = _FakeConn()
    opened_uris: list[str] = []

    def open_connection(uri: str) -> _FakeConn:
        opened_uris.append(uri)
        return conn

    connections = RemoteLibvirtConnections(
        config_factory=lambda: config,
        open_connection=open_connection,
        secret_backend_factory=_SecretBackend,
        pki_base_dir=tmp_path,
    )

    assert connections.config() is config
    with connections.connection(config) as opened:
        assert opened is conn
        assert "pkipath=" in opened_uris[0]
        assert (tmp_path / "kdive-remote-pki-").exists() is False
        pki_dir = next(tmp_path.iterdir())
        assert (pki_dir / "clientcert.pem").read_text(encoding="utf-8") == "PEM::client-cert"

    assert conn.closed
    assert list(tmp_path.iterdir()) == []


class _SecretBackend:
    def resolve(self, ref: str) -> str:
        return f"PEM::{ref.removeprefix('secret://')}"


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True
