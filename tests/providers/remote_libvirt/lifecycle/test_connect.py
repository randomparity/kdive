"""Unit tests for the remote-libvirt Connect plane (issue #205, ADR-0083).

Drive the gdbstub direct-TCP transport orchestration + the full error contract with injected
fakes (config, domain-XML port reader, RSP probe); no libvirt host, no real socket.
"""

from __future__ import annotations

from typing import cast

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.handles import (
    SystemHandle,
    TransportHandle,
)
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect
from kdive.providers.shared.libvirt_xml import QEMU_NS as _QEMU_NS
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend
from tests.providers.remote_libvirt.conftest import RecordingBackend, libvirt_error

_REFS = TlsCertRefs(client_cert_ref="c", client_key_ref="k", ca_cert_ref="a")


def _config(
    *,
    gdb_addr: str | None = "10.0.0.5",
    ssh_addr: str | None = None,
    ssh_port_min: int | None = None,
    ssh_port_max: int | None = None,
) -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://h/system",
        cert_refs=_REFS,
        concurrent_allocation_cap=1,
        gdb_addr=gdb_addr,
        ssh_addr=ssh_addr,
        ssh_port_min=ssh_port_min,
        ssh_port_max=ssh_port_max,
    )


def _connect(*, resolve_port, probe, config: RemoteLibvirtConfig | None = None):
    return RemoteLibvirtConnect(
        config_factory=lambda: config if config is not None else _config(),
        resolve_port=resolve_port,
        probe=probe,
    )


def test_open_gdbstub_returns_handle_for_reachable_stub():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    handle = c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    decoded = TransportHandleData.decode(handle)
    assert (decoded.kind, decoded.host, decoded.port) == ("gdbstub", "10.0.0.5", 47002)


def test_open_gdbstub_resolves_port_and_probes_with_composed_endpoint():
    seen: dict[str, object] = {}

    def resolve_port(system: SystemHandle) -> int:
        seen["resolved_system"] = str(system)
        return 47002

    def probe(host: str, port: int) -> bool:
        seen["probe_host"] = host
        seen["probe_port"] = port
        return True

    c = _connect(resolve_port=resolve_port, probe=probe)
    c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    # The port is resolved from the requested system; the probe targets the ACL'd host
    # (gdb_addr) and the resolved port.
    assert seen == {
        "resolved_system": "kdive-sys",
        "probe_host": "10.0.0.5",
        "probe_port": 47002,
    }


def test_from_env_threads_passed_config_factory():
    factory = lambda: _config()  # noqa: E731 - terse fixture factory
    c = RemoteLibvirtConnect.from_env(secret_registry=SecretRegistry(), config_factory=factory)
    # from_env must pass the caller's factory through, not drop it.
    assert c._config_factory is factory


def _domain_xml(ssh_port: int, *, ssh_addr: str = "10.0.0.9") -> str:
    return (
        f"<domain xmlns:qemu='{_QEMU_NS}'><qemu:commandline>"
        "<qemu:arg value='-netdev'/>"
        f"<qemu:arg value='user,id=kdivessh,restrict=on,hostfwd=tcp:{ssh_addr}:{ssh_port}-:22'/>"
        "</qemu:commandline></domain>"
    )


class _SshReadDomain:
    def __init__(self, xml: str) -> None:
        self._xml = xml

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - libvirt API name
        return self._xml


class _SshReadConn:
    """A minimal connection returning a canned domain XML for one lookupByName."""

    def __init__(self, xml_by_name: dict[str, str]) -> None:
        self._xml_by_name = xml_by_name

    def lookupByName(self, name: str) -> _SshReadDomain:  # noqa: N802 - libvirt API name
        if name not in self._xml_by_name:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return _SshReadDomain(self._xml_by_name[name])

    def close(self) -> None:
        pass


def _ssh_connect(xml_by_name: dict[str, str], config: RemoteLibvirtConfig) -> RemoteLibvirtConnect:
    return RemoteLibvirtConnect(
        config_factory=lambda: config,
        open_connection=lambda uri: _SshReadConn(xml_by_name),
        secret_backend_factory=lambda: cast(SecretBackend, RecordingBackend()),
    )


_DOMAIN = "kdive-00000000-0000-0000-0000-0000000000aa"


def test_recorded_ssh_endpoint_reads_port_from_live_xml() -> None:
    config = _config(ssh_addr="10.0.0.9", ssh_port_min=47100, ssh_port_max=47199)
    connect = _ssh_connect({_DOMAIN: _domain_xml(47101)}, config)

    endpoint = connect.recorded_ssh_endpoint(SystemHandle(_DOMAIN))

    assert endpoint == ("10.0.0.9", 47101)


def test_recorded_ssh_endpoint_none_when_parity_inactive() -> None:
    connect = RemoteLibvirtConnect(config_factory=_config)  # no ssh_addr
    assert connect.recorded_ssh_endpoint(SystemHandle("kdive-x")) is None


def test_recorded_ssh_endpoint_is_a_real_read_not_missing_dependency() -> None:
    # Regression for the challenge finding: recorded_ssh_endpoint must NOT copy the gdb
    # resolve_port stub that raises MISSING_DEPENDENCY — it runs on the live worker.
    config = _config(ssh_addr="10.0.0.9", ssh_port_min=47100, ssh_port_max=47199)
    connect = _ssh_connect({_DOMAIN: _domain_xml(47101)}, config)
    assert connect.recorded_ssh_endpoint(SystemHandle(_DOMAIN)) is not None


def test_recorded_ssh_endpoint_none_for_absent_domain() -> None:
    config = _config(ssh_addr="10.0.0.9", ssh_port_min=47100, ssh_port_max=47199)
    connect = _ssh_connect({}, config)  # no such domain
    assert connect.recorded_ssh_endpoint(SystemHandle("kdive-missing")) is None


def test_open_gdbstub_unset_gdb_addr_is_configuration_error():
    c = _connect(
        resolve_port=lambda system: 47002,
        probe=lambda host, port: True,
        config=_config(gdb_addr=None),
    )
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == ("remote gdbstub host (instance gdb_addr in systems.toml) is unset")


def test_open_gdbstub_unreachable_is_debug_attach_failure():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: False)
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert str(exc.value) == "remote gdbstub did not answer RSP framing"
    assert exc.value.details == {"host": "10.0.0.5", "port": 47002}


def test_open_gdbstub_socket_fault_is_transport_failure():
    def boom(host: str, port: int) -> bool:
        raise OSError("connection refused")

    c = _connect(resolve_port=lambda system: 47002, probe=boom)
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert str(exc.value) == "gdbstub transport socket fault"
    assert exc.value.details == {"port": 47002}


def test_unknown_kind_is_configuration_error():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "ssh")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "unsupported transport kind: 'ssh'"


def test_open_transport_drgn_live_returns_bare_domain_handle():
    # ADR-0083 §4: in-guest drgn rides the guest agent keyed by domain; the handle IS the
    # bare domain name core derived. No gdb_addr needed, no port resolution, no probe.
    c = _connect(
        resolve_port=lambda system: pytest.fail("must not resolve a port for drgn-live"),
        probe=lambda host, port: pytest.fail("must not probe for drgn-live"),
        config=_config(gdb_addr=None),
    )
    handle = c.open_transport(SystemHandle("kdive-remote-1"), "drgn-live")
    assert str(handle) == "kdive-remote-1"


def test_close_transport_no_ops_on_bare_domain_handle():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    c.close_transport(TransportHandle("kdive-remote-1"))  # bare domain, connectionless: no raise


def test_close_transport_still_validates_schemed_gdbstub_handle():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    c.close_transport(TransportHandleData(kind="gdbstub", host="10.0.0.5", port=47002).encode())
    with pytest.raises(CategorizedError):
        c.close_transport(TransportHandle("gdbstub://"))  # schemed but malformed → rejected
