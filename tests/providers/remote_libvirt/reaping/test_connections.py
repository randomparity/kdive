"""Remote-libvirt reaper connection assembly tests."""

from __future__ import annotations

from typing import Any, cast

import pytest

from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.reaping import connections
from kdive.security.secrets.secret_registry import SecretRegistry


class _Conn:
    def close(self) -> None:
        pass


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://builder.example/system",
        cert_refs=TlsCertRefs(
            client_cert_ref="remote/clientcert.pem",
            client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
            ca_cert_ref="remote/cacert.pem",
        ),
        concurrent_allocation_cap=1,
    )


def test_open_libvirt_reaper_uses_protocol_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    conn = _Conn()

    def open_protocol(uri: str) -> _Conn:
        opened.append(uri)
        return conn

    monkeypatch.setattr(connections, "open_libvirt_protocol", open_protocol)

    assert connections.open_libvirt_reaper("qemu+tls://builder.example/system") is conn
    assert opened == ["qemu+tls://builder.example/system"]


def test_reaper_connections_bind_fleet_configs_and_opener() -> None:
    config = _config()
    opened: list[str] = []
    conn = _Conn()

    def open_connection(uri: str) -> _Conn:
        opened.append(uri)
        return conn

    bundle = connections.remote_libvirt_reaper_connections(
        secret_registry=SecretRegistry(),
        open_connection=cast(Any, open_connection),
        configs_factory=lambda: [config],
    )

    assert bundle.configs() == [config]
    assert bundle.open_connection("qemu+tls://builder.example/system") is conn
    assert opened == ["qemu+tls://builder.example/system"]


def test_reaper_bundle_has_no_single_host() -> None:
    # A reaper bundle is fleet-wide; calling the single-host config() must fail loudly so a
    # reaper can never silently sweep just one host (ADR-0187, #395).
    bundle = connections.remote_libvirt_reaper_connections(
        secret_registry=SecretRegistry(),
        open_connection=cast(Any, lambda _uri: _Conn()),
        configs_factory=lambda: [_config()],
    )
    with pytest.raises(AssertionError):
        bundle.config()
