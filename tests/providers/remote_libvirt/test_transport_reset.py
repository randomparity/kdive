"""RemoteLibvirtTransportResetter tests — injected TLS opener + fake conn, no live host."""

from __future__ import annotations

import asyncio
from pathlib import Path

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.transport_reset import RemoteLibvirtTransportResetter
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend
from tests.providers.remote_libvirt.fakes import FakeControlConn, FakeDomain

_GDB_ADDR = "10.0.0.5"
_GDB_ADDR_B = "10.0.0.6"
_DOMAIN = "kdive-sys"


def _config(gdb_addr: str = _GDB_ADDR, host: str = "host.example") -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri=f"qemu+tls://{host}/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
        gdb_addr=gdb_addr,
    )


def _resetter(domain: FakeDomain | None, tmp_path: Path) -> RemoteLibvirtTransportResetter:
    conn = FakeControlConn({_DOMAIN: domain} if domain is not None else {})
    return RemoteLibvirtTransportResetter(
        secret_registry=SecretRegistry(),
        configs_factory=lambda: [_config()],
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )


def test_matching_gdbstub_handle_rearms_with_stop_then_start(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub",
            transport_handle=f"gdbstub://{_GDB_ADDR}:1234",
            domain_name=_DOMAIN,
        )

    asyncio.run(scenario())
    assert domain.calls == ["monitor:gdbserver none", "monitor:gdbserver tcp::1234"]


def test_non_gdbstub_transport_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="drgn-live", transport_handle=_DOMAIN, domain_name=_DOMAIN
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_handle_host_not_gdb_addr_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub",
            transport_handle="gdbstub://127.0.0.1:1234",  # a local loopback session, not ours
            domain_name=_DOMAIN,
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_decoded_non_gdbstub_handle_does_not_load_remote_config(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)
    conn = FakeControlConn({_DOMAIN: domain})

    def unavailable_configs() -> list[RemoteLibvirtConfig]:
        raise AssertionError("config should not load for non-gdbstub handle kinds")

    resetter = RemoteLibvirtTransportResetter(
        secret_registry=SecretRegistry(),
        configs_factory=unavailable_configs,
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )

    async def scenario() -> None:
        await resetter.reset(
            transport="gdbstub",
            transport_handle="ssh://remote.example:22",
            domain_name=_DOMAIN,
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_missing_domain_name_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub", transport_handle=f"gdbstub://{_GDB_ADDR}:1234", domain_name=None
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_none_handle_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub", transport_handle=None, domain_name=_DOMAIN
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_fleet_handle_rearms_on_the_host_whose_gdb_addr_matches(tmp_path: Path) -> None:
    # Two declared hosts (gdb_addr A and B); a handle that encodes host B must re-arm over
    # host B's URI — the resetter self-selects the matching config from the fleet (ADR-0187).
    domain = FakeDomain(_DOMAIN)
    conn = FakeControlConn({_DOMAIN: domain})
    opened: list[str] = []

    def open_connection(uri: str) -> FakeControlConn:
        opened.append(uri)
        return conn

    resetter = RemoteLibvirtTransportResetter(
        secret_registry=SecretRegistry(),
        configs_factory=lambda: [
            _config(_GDB_ADDR, host="host-a.example"),
            _config(_GDB_ADDR_B, host="host-b.example"),
        ],
        open_connection=open_connection,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )

    async def scenario() -> None:
        await resetter.reset(
            transport="gdbstub",
            transport_handle=f"gdbstub://{_GDB_ADDR_B}:1234",
            domain_name=_DOMAIN,
        )

    asyncio.run(scenario())
    assert domain.calls == ["monitor:gdbserver none", "monitor:gdbserver tcp::1234"]
    assert len(opened) == 1
    assert opened[0].startswith("qemu+tls://host-b.example/system")


def test_fleet_handle_matching_no_host_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)
    conn = FakeControlConn({_DOMAIN: domain})

    resetter = RemoteLibvirtTransportResetter(
        secret_registry=SecretRegistry(),
        configs_factory=lambda: [
            _config(_GDB_ADDR, host="host-a.example"),
            _config(_GDB_ADDR_B, host="host-b.example"),
        ],
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )

    async def scenario() -> None:
        await resetter.reset(
            transport="gdbstub",
            transport_handle="gdbstub://10.9.9.9:1234",  # neither host's gdb_addr
            domain_name=_DOMAIN,
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_monitor_error_maps_to_transport_failure(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN, raise_on={"qemuMonitorCommand": libvirt.VIR_ERR_OPERATION_FAILED})

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub",
            transport_handle=f"gdbstub://{_GDB_ADDR}:1234",
            domain_name=_DOMAIN,
        )

    with pytest.raises(CategorizedError) as exc:
        asyncio.run(scenario())
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
