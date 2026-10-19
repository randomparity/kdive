"""Connect-plane endpoint, access-policy and SSH tests (no live_vm).

Gdbstub reservation is metadata-only; real libvirt/SSH seams are injected.
"""

from __future__ import annotations

from typing import cast

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle import connect as connect_mod
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.ports.handles import SystemHandle
from kdive.providers.ports.lifecycle import (
    DebugTransportKind,
    TransportHandleData,
)
from tests.providers.local_libvirt.fakes import libvirt_error

# --- TransportHandleData codec -------------------------------------------------------------


def test_transport_handle_roundtrips() -> None:
    handle = TransportHandleData(kind="gdbstub", host="127.0.0.1", port=1234)
    encoded = handle.encode()
    assert TransportHandleData.decode(encoded) == handle


def test_transport_handle_decode_malformed_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode("not-a-handle")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_ssh_transport_handle_roundtrips() -> None:
    handle = TransportHandleData(kind="ssh", host="127.0.0.1", port=22)
    encoded = handle.encode()
    assert encoded.startswith("ssh://")
    assert TransportHandleData.decode(encoded) == handle


def test_transport_handle_decode_unknown_scheme_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode("telnet://127.0.0.1:23")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_drgn_live_transport_handle_roundtrips() -> None:
    # fault-inject emits a drgn-live:// scheme handle, so decode must accept it (#215).
    handle = TransportHandleData(kind="drgn-live", host="127.0.0.1", port=1234)
    encoded = handle.encode()
    assert encoded.startswith("drgn-live://")
    assert TransportHandleData.decode(encoded) == handle


# --- Connector orchestration ---------------------------------------------------------------


def _connector(*, host: str = "127.0.0.1", port: int = 1234) -> LocalLibvirtConnect:
    return LocalLibvirtConnect(resolve_endpoint=lambda _system: (host, port))


_SYSTEM = SystemHandle("kdive-x")


def test_open_transport_non_gdbstub_kind_is_configuration_error_without_probing() -> None:
    with pytest.raises(CategorizedError) as exc:
        _connector().open_transport(_SYSTEM, cast(DebugTransportKind, "tcp"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # The message names the offending kind so the operator sees what was rejected.
    assert str(exc.value) == "unsupported transport kind: 'tcp'"


def test_open_gdbstub_resolves_endpoint_for_the_requested_system() -> None:
    seen: list[SystemHandle] = []

    def resolver(system: SystemHandle) -> tuple[str, int]:
        seen.append(system)
        return ("127.0.0.1", 1234)

    connector = LocalLibvirtConnect(resolve_endpoint=resolver)
    connector.open_transport(_SYSTEM, "gdbstub")
    assert seen == [_SYSTEM]


def test_open_transport_non_loopback_host_is_configuration_error_without_probing() -> None:
    with pytest.raises(CategorizedError) as exc:
        _connector(host="10.0.0.1").open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "gdbstub host must be a loopback IP literal"


def test_open_transport_hostname_host_is_configuration_error_without_probing() -> None:
    with pytest.raises(CategorizedError) as exc:
        _connector(host="evil.example").open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_open_transport_resolved_endpoint_returns_decodable_handle() -> None:
    handle = _connector().open_transport(_SYSTEM, "gdbstub")
    decoded = TransportHandleData.decode(str(handle))
    assert decoded == TransportHandleData(kind="gdbstub", host="127.0.0.1", port=1234)


class _FakeGdbDomain:
    """A libvirt domain whose XMLDesc records (or omits) a gdbstub port."""

    def __init__(self, xml: str) -> None:
        self._xml = xml

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - mirrors the libvirt binding name
        return self._xml


class _FakeGdbConn:
    """A libvirt connection that resolves one domain by name (or raises a libvirtError)."""

    def __init__(
        self, *, domain: _FakeGdbDomain | None = None, error_code: int | None = None
    ) -> None:
        self._domain = domain
        self._error_code = error_code
        self.closed = 0

    def lookupByName(self, name: str) -> _FakeGdbDomain:  # noqa: N802 - libvirt binding name
        if self._error_code is not None:
            raise libvirt_error(self._error_code)
        assert self._domain is not None
        return self._domain

    def close(self) -> int:
        self.closed += 1
        return 0


def _gdb_xml(port: int) -> str:
    from kdive.providers.shared.libvirt_xml import QEMU_NS

    return (
        f"<domain xmlns:qemu='{QEMU_NS}'><qemu:commandline>"
        "<qemu:arg value='-gdb'/>"
        f"<qemu:arg value='tcp:127.0.0.1:{port}'/>"
        "</qemu:commandline></domain>"
    )


def test_resolve_endpoint_reads_the_recorded_port_from_the_live_domain() -> None:
    conn = _FakeGdbConn(domain=_FakeGdbDomain(_gdb_xml(4444)))
    resolver = connect_mod._resolve_endpoint_via(lambda: conn)
    assert resolver(_SYSTEM) == ("127.0.0.1", 4444)
    assert conn.closed == 1  # the connection is always closed


def test_resolve_endpoint_absent_domain_is_configuration_error() -> None:
    conn = _FakeGdbConn(error_code=libvirt.VIR_ERR_NO_DOMAIN)
    resolver = connect_mod._resolve_endpoint_via(lambda: conn)
    with pytest.raises(CategorizedError) as exc:
        resolver(_SYSTEM)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.closed == 1


def test_resolve_endpoint_without_a_recorded_port_is_configuration_error() -> None:
    # A System provisioned without debug.gdbstub records no port — actionable, not a missing dep.
    conn = _FakeGdbConn(domain=_FakeGdbDomain("<domain/>"))
    resolver = connect_mod._resolve_endpoint_via(lambda: conn)
    with pytest.raises(CategorizedError) as exc:
        resolver(_SYSTEM)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_endpoint_malformed_xml_is_infrastructure_failure() -> None:
    conn = _FakeGdbConn(domain=_FakeGdbDomain("<domain"))
    resolver = connect_mod._resolve_endpoint_via(lambda: conn)
    with pytest.raises(CategorizedError) as exc:
        resolver(_SYSTEM)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_resolve_endpoint_other_libvirt_error_is_infrastructure_failure() -> None:
    conn = _FakeGdbConn(error_code=libvirt.VIR_ERR_INTERNAL_ERROR)
    resolver = connect_mod._resolve_endpoint_via(lambda: conn)
    with pytest.raises(CategorizedError) as exc:
        resolver(_SYSTEM)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_close_transport_is_noop_and_never_raises() -> None:
    connector = _connector()
    handle = connector.open_transport(_SYSTEM, "gdbstub")
    connector.close_transport(handle)  # no raise


def test_close_transport_rejects_malformed_handle() -> None:
    connector = connect_mod.LocalLibvirtConnect(resolve_endpoint=lambda _s: ("127.0.0.1", 1))
    with pytest.raises(CategorizedError) as exc:
        connector.close_transport(connect_mod.TransportHandle("garbage"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- SSH transport orchestration (ADR-0039) ------------------------------------------------


class _FakeSshConnect:
    """Records (host, port) calls; returns True or raises a canned error."""

    def __init__(self, *, result: bool = True, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> bool:
        self.calls.append((host, port))
        if self._raises is not None:
            raise self._raises
        return self._result


def _ssh_connector(
    ssh_connect: _FakeSshConnect, *, host: str = "127.0.0.1", port: int = 22
) -> LocalLibvirtConnect:
    return LocalLibvirtConnect(
        resolve_endpoint=lambda _system: ("127.0.0.1", 1234),
        resolve_ssh_endpoint=lambda _system: (host, port),
        ssh_connect=ssh_connect,
    )


def test_open_ssh_transport_returns_decodable_ssh_handle() -> None:
    ssh = _FakeSshConnect(result=True)
    handle = _ssh_connector(ssh).open_transport(_SYSTEM, "drgn-live")
    decoded = TransportHandleData.decode(str(handle))
    assert decoded == TransportHandleData(kind="ssh", host="127.0.0.1", port=22)
    assert ssh.calls == [("127.0.0.1", 22)]


def test_open_ssh_transport_non_loopback_host_is_configuration_error_without_io() -> None:
    ssh = _FakeSshConnect()
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(ssh, host="10.0.0.1").open_transport(_SYSTEM, "drgn-live")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "ssh host must be a loopback IP literal"
    assert ssh.calls == []  # F2: no outbound SSH connect to a non-loopback host


def test_open_ssh_transport_hostname_host_is_configuration_error_without_io() -> None:
    ssh = _FakeSshConnect()
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(ssh, host="guest.example").open_transport(_SYSTEM, "drgn-live")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ssh.calls == []  # a hostname is not a loopback IP literal — reject without DNS


def test_open_ssh_transport_unreachable_is_debug_attach_failure() -> None:
    ssh = _FakeSshConnect(result=False)
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(ssh, port=2222).open_transport(_SYSTEM, "drgn-live")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert str(exc.value) == "ssh endpoint did not accept a connection"
    assert exc.value.details == {"port": 2222}


def test_open_ssh_transport_socket_fault_is_transport_failure() -> None:
    ssh = _FakeSshConnect(raises=OSError("connection reset"))
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(ssh, port=2222).open_transport(_SYSTEM, "drgn-live")
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert str(exc.value) == "ssh transport socket fault"
    assert exc.value.details == {"port": 2222}


def test_open_ssh_resolves_endpoint_for_the_requested_system() -> None:
    seen: list[SystemHandle] = []

    def ssh_resolver(system: SystemHandle) -> tuple[str, int]:
        seen.append(system)
        return ("127.0.0.1", 22)

    connector = LocalLibvirtConnect(
        resolve_endpoint=lambda _s: ("127.0.0.1", 1234),
        resolve_ssh_endpoint=ssh_resolver,
        ssh_connect=_FakeSshConnect(result=True),
    )
    connector.open_transport(_SYSTEM, "drgn-live")
    assert seen == [_SYSTEM]


def test_open_unsupported_kind_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(_FakeSshConnect()).open_transport(
            _SYSTEM, cast(DebugTransportKind, "telnet")
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "unsupported transport kind: 'telnet'"


# --- SSH banner reachability probe (ADR-0218/0039) -----------------------------------------


def test_ssh_banner_verdict_accepts_openssh_identification() -> None:
    assert connect_mod._ssh_banner_verdict(b"SSH-2.0-OpenSSH_9.6\r\n") is True


def test_ssh_banner_verdict_accepts_bare_prefix() -> None:
    # The prefix alone is enough — a live sshd has identified itself as speaking SSH.
    assert connect_mod._ssh_banner_verdict(b"SSH-") is True


def test_ssh_banner_verdict_undecided_while_still_a_prefix() -> None:
    # Partial reads that could still complete to "SSH-" keep the probe reading.
    assert connect_mod._ssh_banner_verdict(b"") is None
    assert connect_mod._ssh_banner_verdict(b"S") is None
    assert connect_mod._ssh_banner_verdict(b"SSH") is None


def test_ssh_banner_verdict_rejects_non_ssh_listener() -> None:
    # A listener that accepts TCP but speaks something else is rejected once the first bytes
    # diverge from "SSH-".
    assert connect_mod._ssh_banner_verdict(b"220 smtp ready\r\n") is False
    assert connect_mod._ssh_banner_verdict(b"SSX") is False


class _FakeSshSocket:
    """A connected socket that yields canned recv chunks, then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = [*chunks, b""]

    def settimeout(self, _timeout: float) -> None:
        return None

    def recv(self, _size: int) -> bytes:
        return self._chunks.pop(0)

    def close(self) -> None:
        return None


class _FailingSshSocket(_FakeSshSocket):
    def __init__(self) -> None:
        super().__init__([])

    def recv(self, _size: int) -> bytes:
        raise OSError("recv failed")


def test_real_ssh_connect_raises_when_connection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(address: tuple[str, int], *, timeout: float) -> object:
        assert address == ("127.0.0.1", 2222)
        assert timeout > 0
        raise OSError("connection refused")

    monkeypatch.setattr(connect_mod.socket, "create_connection", fail_connect)
    with pytest.raises(OSError, match="connection refused"):
        connect_mod._real_ssh_connect("127.0.0.1", 2222)


def test_real_ssh_connect_raises_when_recv_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        connect_mod.socket,
        "create_connection",
        lambda _addr, *, timeout: _FailingSshSocket(),
    )
    with pytest.raises(OSError, match="recv failed"):
        connect_mod._real_ssh_connect("127.0.0.1", 2222)


def test_open_ssh_transport_real_probe_connection_refused_is_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(address: tuple[str, int], *, timeout: float) -> object:
        assert address == ("127.0.0.1", 2222)
        assert timeout > 0
        raise OSError("connection refused")

    monkeypatch.setattr(connect_mod.socket, "create_connection", fail_connect)
    connector = LocalLibvirtConnect(
        resolve_endpoint=lambda _system: ("127.0.0.1", 1234),
        resolve_ssh_endpoint=lambda _system: ("127.0.0.1", 2222),
        ssh_connect=connect_mod._real_ssh_connect,
    )

    with pytest.raises(CategorizedError) as exc:
        connector.open_transport(_SYSTEM, "drgn-live")

    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert str(exc.value) == "ssh transport socket fault"
    assert exc.value.details == {"port": 2222}


def test_real_ssh_connect_true_when_peer_sends_ssh_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connect_mod.socket,
        "create_connection",
        lambda _addr, *, timeout: _FakeSshSocket([b"SSH-2.0-OpenSSH_9.6\r\n"]),
    )
    assert connect_mod._real_ssh_connect("127.0.0.1", 2222) is True


def test_real_ssh_connect_false_when_peer_is_not_ssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connect_mod.socket,
        "create_connection",
        lambda _addr, *, timeout: _FakeSshSocket([b"220 smtp ready\r\n"]),
    )
    assert connect_mod._real_ssh_connect("127.0.0.1", 2222) is False


def test_real_ssh_connect_false_when_peer_sends_no_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A listener that accepts then immediately closes (EOF) without a banner is rejected.
    monkeypatch.setattr(
        connect_mod.socket, "create_connection", lambda _addr, *, timeout: _FakeSshSocket([])
    )
    assert connect_mod._real_ssh_connect("127.0.0.1", 2222) is False


def _ssh_xml(port: int) -> str:
    from kdive.providers.shared.libvirt_xml import QEMU_NS

    return (
        f"<domain xmlns:qemu='{QEMU_NS}'><qemu:commandline>"
        "<qemu:arg value='-netdev'/>"
        f"<qemu:arg value='user,id=kdivessh,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:22'/>"
        "</qemu:commandline></domain>"
    )


def test_resolve_ssh_endpoint_reads_the_recorded_port_from_the_live_domain() -> None:
    conn = _FakeGdbConn(domain=_FakeGdbDomain(_ssh_xml(40022)))
    resolver = connect_mod._resolve_ssh_endpoint_via(lambda: conn)
    assert resolver(_SYSTEM) == ("127.0.0.1", 40022)
    assert conn.closed == 1  # the connection is always closed


def test_resolve_ssh_endpoint_absent_domain_is_configuration_error() -> None:
    conn = _FakeGdbConn(error_code=libvirt.VIR_ERR_NO_DOMAIN)
    resolver = connect_mod._resolve_ssh_endpoint_via(lambda: conn)
    with pytest.raises(CategorizedError) as exc:
        resolver(_SYSTEM)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # The reason tag is what keeps this apart from "no recorded forward" downstream (#2502).
    assert exc.value.details["reason"] == "system_domain_not_found"
    # The message names the check #2480 needed: a server on a different libvirt endpoint than
    # the worker finds no domain for a healthy System.
    assert "libvirt endpoint" in str(exc.value)
    assert conn.closed == 1


def test_resolve_ssh_endpoint_without_a_recorded_port_is_configuration_error() -> None:
    # A domain with no recorded loopback SSH forward is actionable (reprovision), not a missing dep.
    conn = _FakeGdbConn(domain=_FakeGdbDomain("<domain/>"))
    resolver = connect_mod._resolve_ssh_endpoint_via(lambda: conn)
    with pytest.raises(CategorizedError) as exc:
        resolver(_SYSTEM)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["reason"] == "ssh_not_provisioned"
    assert "SSH forward" in str(exc.value)
    assert "reprovision" in str(exc.value)


def test_resolve_ssh_endpoint_malformed_xml_is_infrastructure_failure() -> None:
    conn = _FakeGdbConn(domain=_FakeGdbDomain("<domain"))
    resolver = connect_mod._resolve_ssh_endpoint_via(lambda: conn)
    with pytest.raises(CategorizedError) as exc:
        resolver(_SYSTEM)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_resolve_ssh_endpoint_other_libvirt_error_is_infrastructure_failure() -> None:
    conn = _FakeGdbConn(error_code=libvirt.VIR_ERR_INTERNAL_ERROR)
    resolver = connect_mod._resolve_ssh_endpoint_via(lambda: conn)
    with pytest.raises(CategorizedError) as exc:
        resolver(_SYSTEM)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_real_resolve_ssh_endpoint_is_wired_not_the_deferred_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The module-level `_real_resolve_ssh_endpoint` (the one `from_env` wires) is now the real
    # resolver over `_default_connect`, not the old `#697`-deferred stub. Stub the libvirt `open`
    # seam (the only `live_vm` boundary) with a fake recording the forwarded port so the test never
    # touches a real libvirt socket — a real `libvirt.open` here both flakes off-host and pollutes
    # process-global libvirt error state for later tests. Calling the resolver directly proves the
    # wiring without invoking the `live_vm`-gated SSH probe.
    conn = _FakeGdbConn(domain=_FakeGdbDomain(_ssh_xml(40022)))
    monkeypatch.setattr(connect_mod.libvirt, "open", lambda _uri: conn)
    monkeypatch.setattr(connect_mod.config, "require", lambda _setting: "qemu:///system")
    assert connect_mod._real_resolve_ssh_endpoint(_SYSTEM) == ("127.0.0.1", 40022)


def test_close_ssh_transport_is_noop_and_never_raises() -> None:
    connector = _ssh_connector(_FakeSshConnect())
    handle = connector.open_transport(_SYSTEM, "drgn-live")
    connector.close_transport(handle)  # no raise


def test_open_transport_accepts_drgn_live_kind_and_emits_ssh_scheme_handle() -> None:
    # The agent-facing token is `drgn-live`; the local realization is SSH, so the handle
    # scheme stays `ssh://` (a provider-internal detail core treats as opaque, #215/ADR-0085).
    ssh = _FakeSshConnect(result=True)
    handle = _ssh_connector(ssh).open_transport(_SYSTEM, "drgn-live")
    assert str(handle).startswith("ssh://")
    assert TransportHandleData.decode(str(handle)) == TransportHandleData(
        kind="ssh", host="127.0.0.1", port=22
    )


def _recorded_endpoint_connector(resolve_ssh_endpoint):
    return LocalLibvirtConnect(
        resolve_endpoint=lambda _s: ("127.0.0.1", 1),
        resolve_ssh_endpoint=resolve_ssh_endpoint,
        ssh_connect=lambda _h, _p: True,
    )


def test_recorded_ssh_endpoint_returns_host_port() -> None:
    connector = _recorded_endpoint_connector(lambda _s: ("127.0.0.1", 22022))
    assert connector.recorded_ssh_endpoint(SystemHandle("sys-1")) == ("127.0.0.1", 22022)


def test_recorded_ssh_endpoint_none_when_not_provisioned() -> None:
    def _raise(_s):
        raise CategorizedError(
            "no ssh",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "ssh_not_provisioned"},
        )

    connector = _recorded_endpoint_connector(_raise)
    assert connector.recorded_ssh_endpoint(SystemHandle("sys-1")) is None


def test_recorded_ssh_endpoint_reraises_absent_domain() -> None:
    """A missing domain is a different fault from a missing forward, so it must not become None.

    Collapsing it is what let #2480 surface an endpoint-resolution defect as an SSH provisioning
    gap (#2502, ADR-0658).
    """

    def _raise(_s):
        raise CategorizedError(
            "System 'sys-1' has no libvirt domain on this connection",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "system_domain_not_found"},
        )

    connector = _recorded_endpoint_connector(_raise)
    with pytest.raises(CategorizedError) as excinfo:
        connector.recorded_ssh_endpoint(SystemHandle("sys-1"))
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "system_domain_not_found"


def test_recorded_ssh_endpoint_reraises_untagged_configuration_error() -> None:
    """Only the no-forward reason maps to None; anything else propagates rather than lying."""

    def _raise(_s):
        raise CategorizedError("something else", category=ErrorCategory.CONFIGURATION_ERROR)

    connector = _recorded_endpoint_connector(_raise)
    with pytest.raises(CategorizedError):
        connector.recorded_ssh_endpoint(SystemHandle("sys-1"))


def test_recorded_ssh_endpoint_reraises_non_configuration_carrying_the_forward_reason() -> None:
    """The category conjunct is load-bearing, not redundant with the reason.

    `None` is documented as narrowing a CONFIGURATION_ERROR (ADR-0298, ADR-0658). A fault in any
    other category that happens to carry the same reason string must not be reported to the caller
    as "this System has no SSH forward".
    """

    def _raise(_s):
        raise CategorizedError(
            "libvirt read fault",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"reason": "ssh_not_provisioned"},
        )

    connector = _recorded_endpoint_connector(_raise)
    with pytest.raises(CategorizedError) as excinfo:
        connector.recorded_ssh_endpoint(SystemHandle("sys-1"))
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_recorded_ssh_endpoint_reraises_infrastructure_failure() -> None:
    def _raise(_s):
        raise CategorizedError("libvirt down", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    connector = _recorded_endpoint_connector(_raise)
    with pytest.raises(CategorizedError) as excinfo:
        connector.recorded_ssh_endpoint(SystemHandle("sys-1"))
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize("initial_state", ["running", "paused"])
@pytest.mark.parametrize("boundary", ["create_connection", "connect"])
def test_gdbstub_reservation_never_dials(
    monkeypatch: pytest.MonkeyPatch, initial_state: str, boundary: str
) -> None:
    import socket

    state = [initial_state]
    calls: list[object] = []

    def intrusive_dial(*args: object, **kwargs: object) -> None:
        calls.append(args)
        state[0] = "paused"
        pytest.fail("session reservation attempted a standalone debugger connection")

    target = socket if boundary == "create_connection" else socket.socket
    monkeypatch.setattr(target, boundary, intrusive_dial)
    conn = _FakeGdbConn(domain=_FakeGdbDomain(_gdb_xml(4444)))
    monkeypatch.setattr(connect_mod, "_default_connect", lambda: conn)
    connector = LocalLibvirtConnect.from_env()
    handle = connector.open_transport(_SYSTEM, "gdbstub")
    connector.close_transport(handle)
    assert TransportHandleData.decode(handle).port == 4444
    assert calls == []
    assert state == [initial_state]
    assert conn.closed == 1
