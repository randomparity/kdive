"""Remote-libvirt reaper connection assembly tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.reaping import connections
from kdive.security.secrets.secret_registry import SecretRegistry


class _Conn:
    def close(self) -> None:
        pass


def _config(uri: str = "qemu+tls://builder.example/system") -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri=uri,
        cert_refs=TlsCertRefs(
            client_cert_ref="remote/clientcert.pem",
            client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
            ca_cert_ref="remote/cacert.pem",
        ),
        concurrent_allocation_cap=1,
    )


class _FakeFleet:
    """A fleet-connections stand-in: each host yields a conn, or raises on connect."""

    def __init__(self, hosts: list[tuple[RemoteLibvirtConfig, object]]) -> None:
        self._hosts = hosts

    def configs(self) -> list[RemoteLibvirtConfig]:
        return [config for config, _ in self._hosts]

    @contextmanager
    def connection(self, config: RemoteLibvirtConfig) -> Iterator[object]:
        conn = next(c for cfg, c in self._hosts if cfg is config)
        if isinstance(conn, Exception):
            raise conn
        yield conn


def _fleet(hosts: list[tuple[RemoteLibvirtConfig, object]]) -> connections.FleetConnections[object]:
    return cast(connections.FleetConnections[object], _FakeFleet(hosts))


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


def test_map_over_fleet_collects_from_every_healthy_host() -> None:
    a, b = _config("qemu+tls://a.example/system"), _config("qemu+tls://b.example/system")
    fleet = _fleet([(a, object()), (b, object())])
    result = connections.map_over_fleet(
        fleet, lambda _conn, config: config.uri, operation="test list"
    )
    assert result == ["qemu+tls://a.example/system", "qemu+tls://b.example/system"]


def test_map_over_fleet_skips_an_unreachable_host() -> None:
    # The realistic degraded-dependency case (ADR-0187, #395): one host is down, the rest are
    # healthy. The sweep must still cover the healthy hosts instead of aborting fleet-wide.
    bad, good = _config("qemu+tls://bad.example/system"), _config("qemu+tls://good.example/system")
    fleet = _fleet([(bad, RuntimeError("unreachable")), (good, object())])
    result = connections.map_over_fleet(
        fleet, lambda _conn, config: config.uri, operation="test list"
    )
    assert result == ["qemu+tls://good.example/system"]


def test_map_over_fleet_passes_each_hosts_connection_to_work() -> None:
    # work must receive the host's open connection (not None), so the reaper can act on it.
    a, b = _config("qemu+tls://a.example/system"), _config("qemu+tls://b.example/system")
    conn_a, conn_b = object(), object()
    fleet = _fleet([(a, conn_a), (b, conn_b)])
    seen: list[object] = []

    def work(conn: object, _config: RemoteLibvirtConfig) -> object:
        seen.append(conn)
        return conn

    result = connections.map_over_fleet(fleet, work, operation="test list")
    assert result == [conn_a, conn_b]
    assert seen == [conn_a, conn_b]


def test_find_over_fleet_passes_the_hosts_connection_to_work() -> None:
    a = _config("qemu+tls://a.example/system")
    conn_a = object()
    fleet = _fleet([(a, conn_a)])
    seen: list[object] = []

    def work(conn: object, _config: RemoteLibvirtConfig) -> bool:
        seen.append(conn)
        return True

    assert connections.find_over_fleet(fleet, work, operation="test delete") is True
    assert seen == [conn_a]


def test_map_over_fleet_propagates_a_work_failure_on_a_reachable_host() -> None:
    # Only an unreachable host is isolated; a genuine error from work() on a reachable host must
    # surface (the reaper's "preserve non-benign failures" contract), not be silently swallowed.
    a = _config("qemu+tls://a.example/system")
    fleet = _fleet([(a, object())])

    def work(_conn: object, _config: RemoteLibvirtConfig) -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        connections.map_over_fleet(fleet, work, operation="test list")


def test_find_over_fleet_skips_unreachable_then_finds_on_healthy_host() -> None:
    bad, good = _config("qemu+tls://bad.example/system"), _config("qemu+tls://good.example/system")
    fleet = _fleet([(bad, RuntimeError("unreachable")), (good, object())])
    visited: list[str] = []

    def work(_conn: object, config: RemoteLibvirtConfig) -> bool:
        visited.append(config.uri)
        return config.uri == "qemu+tls://good.example/system"

    assert connections.find_over_fleet(fleet, work, operation="test delete") is True
    assert visited == ["qemu+tls://good.example/system"]


def test_find_over_fleet_stops_at_the_first_matching_host() -> None:
    a, b = _config("qemu+tls://a.example/system"), _config("qemu+tls://b.example/system")
    fleet = _fleet([(a, object()), (b, object())])
    visited: list[str] = []

    def work(_conn: object, config: RemoteLibvirtConfig) -> bool:
        visited.append(config.uri)
        return True

    assert connections.find_over_fleet(fleet, work, operation="test delete") is True
    assert visited == ["qemu+tls://a.example/system"]


def test_find_over_fleet_returns_false_when_no_host_has_the_target() -> None:
    a, b = _config("qemu+tls://a.example/system"), _config("qemu+tls://b.example/system")
    fleet = _fleet([(a, object()), (b, object())])
    assert connections.find_over_fleet(fleet, lambda _c, _cfg: False, operation="t") is False
