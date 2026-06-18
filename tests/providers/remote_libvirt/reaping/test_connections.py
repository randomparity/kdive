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


def test_reaper_connections_bind_remote_config_and_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    opened: list[str] = []
    conn = _Conn()

    def open_connection(uri: str) -> _Conn:
        opened.append(uri)
        return conn

    monkeypatch.setattr(connections, "remote_config_from_inventory", lambda: config)

    bundle = connections.remote_libvirt_reaper_connections(
        secret_registry=SecretRegistry(),
        open_connection=cast(Any, open_connection),
    )

    assert bundle.config() is config
    assert bundle.open_connection("qemu+tls://builder.example/system") is conn
    assert opened == ["qemu+tls://builder.example/system"]
