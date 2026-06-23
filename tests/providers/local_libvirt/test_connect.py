"""Connect-plane provider tests — RSP framing + the seam-injected Connector (no live_vm).

The RSP-framing codec and the `Connector` orchestration (loopback check, prober dispatch,
error mapping, handle codec) are covered with fakes; the real socket / libvirt-domain
endpoint paths are `live_vm`-gated seams exercised only under the gate.
"""

from __future__ import annotations

from typing import cast

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle import connect as connect_mod
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.ports import DebugTransportKind, SystemHandle, TransportHandleData
from kdive.providers.shared.debug_common import rsp as rsp_mod
from kdive.providers.shared.debug_common.rsp import rsp_frame, valid_rsp_frame
from tests.providers.local_libvirt.fakes import libvirt_error

# --- RSP framing codec ---------------------------------------------------------------------


def test_rsp_frame_wraps_with_mod256_checksum() -> None:
    # '?' is 0x3f; checksum of a single 0x3f payload is 0x3f.
    assert rsp_frame("?") == b"$?#3f"


def test_rsp_frame_checksum_wraps_at_256() -> None:
    # "~~~" sums to 378; mod-256 is 0x7a (122), which a mod-257 mutant would not produce.
    assert rsp_frame("~~~") == b"$~~~#7a"


def test_valid_rsp_frame_accepts_complete_checksum_valid_frame() -> None:
    assert valid_rsp_frame(b"$?#3f") is True


def test_valid_rsp_frame_accepts_wrapped_checksum() -> None:
    # Mirrors rsp_frame's mod-256 wrap so the validator's modulus is pinned too.
    assert valid_rsp_frame(b"$~~~#7a") is True


def test_valid_rsp_frame_ignores_leading_ack() -> None:
    assert valid_rsp_frame(b"+$?#3f") is True


def test_valid_rsp_frame_ignores_leading_nack() -> None:
    # A leading '-' (nack) is an ack byte and is skipped just like '+'.
    assert valid_rsp_frame(b"-$?#3f") is True


def test_valid_rsp_frame_uses_first_hash_as_terminator() -> None:
    # The first '#' terminates the frame. `$a##84` would only validate if the *last* '#'
    # were used (payload "a#" sums to 0x84); using the first '#' it must be rejected.
    assert valid_rsp_frame(b"$a##84") is False


def test_valid_rsp_frame_rejects_trailing_bytes() -> None:
    assert valid_rsp_frame(b"$?#3fJUNK") is False


def test_valid_rsp_frame_rejects_trailing_bytes_after_leading_ack() -> None:
    assert valid_rsp_frame(b"+$?#3fJUNK") is False


def test_valid_rsp_frame_rejects_bare_ack() -> None:
    assert valid_rsp_frame(b"+") is False


def test_valid_rsp_frame_rejects_unterminated_frame() -> None:
    assert valid_rsp_frame(b"$hello") is False


def test_valid_rsp_frame_rejects_non_hex_checksum() -> None:
    assert valid_rsp_frame(b"$?#zz") is False


def test_valid_rsp_frame_rejects_checksum_mismatch() -> None:
    assert valid_rsp_frame(b"$?#00") is False


def test_rsp_reachable_returns_false_when_connection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(address: tuple[str, int], *, timeout: float) -> object:
        assert address == ("127.0.0.1", 1234)
        assert timeout > 0
        raise OSError("connection refused")

    monkeypatch.setattr(rsp_mod.socket, "create_connection", fail_connect)

    assert rsp_mod.rsp_reachable("127.0.0.1", 1234) is False


class _FakeSocket:
    """A connected socket that answers one valid RSP frame, then EOF."""

    def __init__(self) -> None:
        self._chunks = [b"+" + rsp_frame("?"), b""]

    def sendall(self, _data: bytes) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def recv(self, _size: int) -> bytes:
        return self._chunks.pop(0)

    def close(self) -> None:
        return None


def test_rsp_reachable_true_when_peer_answers_valid_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reachable stub that answers a valid frame within the deadline returns True; this also
    # pins the deadline computation (a None/already-elapsed deadline would never read the reply).
    monkeypatch.setattr(
        rsp_mod.socket, "create_connection", lambda _addr, *, timeout: _FakeSocket()
    )
    assert rsp_mod.rsp_reachable("127.0.0.1", 1234) is True


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


class _FakeProbe:
    """Records (host, port) calls; returns a canned result or raises a canned error."""

    def __init__(self, *, result: bool = True, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> bool:
        self.calls.append((host, port))
        if self._raises is not None:
            raise self._raises
        return self._result


def _connector(
    probe: _FakeProbe, *, host: str = "127.0.0.1", port: int = 1234
) -> LocalLibvirtConnect:
    return LocalLibvirtConnect(resolve_endpoint=lambda _system: (host, port), probe=probe)


_SYSTEM = SystemHandle("kdive-x")


def test_open_transport_non_gdbstub_kind_is_configuration_error_without_probing() -> None:
    probe = _FakeProbe()
    with pytest.raises(CategorizedError) as exc:
        _connector(probe).open_transport(_SYSTEM, cast(DebugTransportKind, "tcp"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # The message names the offending kind so the operator sees what was rejected.
    assert str(exc.value) == "unsupported transport kind: 'tcp'"
    assert probe.calls == []  # rejected before any IO


def test_open_gdbstub_resolves_endpoint_for_the_requested_system() -> None:
    seen: list[SystemHandle] = []

    def resolver(system: SystemHandle) -> tuple[str, int]:
        seen.append(system)
        return ("127.0.0.1", 1234)

    probe = _FakeProbe(result=True)
    connector = LocalLibvirtConnect(resolve_endpoint=resolver, probe=probe)
    connector.open_transport(_SYSTEM, "gdbstub")
    assert seen == [_SYSTEM]


def test_open_transport_non_loopback_host_is_configuration_error_without_probing() -> None:
    probe = _FakeProbe()
    with pytest.raises(CategorizedError) as exc:
        _connector(probe, host="10.0.0.1").open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "gdbstub host must be a loopback IP literal"
    assert probe.calls == []  # F2: no outbound connect to a non-loopback host


def test_open_transport_hostname_host_is_configuration_error_without_probing() -> None:
    probe = _FakeProbe()
    with pytest.raises(CategorizedError) as exc:
        _connector(probe, host="evil.example").open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert probe.calls == []  # a hostname is not a loopback IP literal — reject without DNS


def test_open_transport_unreachable_stub_is_debug_attach_failure() -> None:
    probe = _FakeProbe(result=False)
    with pytest.raises(CategorizedError) as exc:
        _connector(probe, port=4242).open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert str(exc.value) == "gdbstub did not answer RSP framing"
    assert exc.value.details == {"port": 4242}
    assert probe.calls == [("127.0.0.1", 4242)]


def test_open_transport_socket_fault_is_transport_failure() -> None:
    probe = _FakeProbe(raises=OSError("connection reset"))
    with pytest.raises(CategorizedError) as exc:
        _connector(probe, port=4242).open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert str(exc.value) == "gdbstub transport socket fault"
    assert exc.value.details == {"port": 4242}


def test_open_transport_reachable_stub_returns_decodable_handle() -> None:
    probe = _FakeProbe(result=True)
    handle = _connector(probe).open_transport(_SYSTEM, "gdbstub")
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
    probe = _FakeProbe()
    connector = _connector(probe)
    handle = connector.open_transport(_SYSTEM, "gdbstub")
    connector.close_transport(handle)  # no raise


def test_close_transport_rejects_malformed_handle() -> None:
    probe = _FakeProbe()
    connector = connect_mod.LocalLibvirtConnect(
        resolve_endpoint=lambda _s: ("127.0.0.1", 1), probe=probe
    )
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
        probe=_FakeProbe(),
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
        probe=_FakeProbe(),
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


def test_real_ssh_connect_false_when_connection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(address: tuple[str, int], *, timeout: float) -> object:
        assert address == ("127.0.0.1", 2222)
        assert timeout > 0
        raise OSError("connection refused")

    monkeypatch.setattr(connect_mod.socket, "create_connection", fail_connect)
    assert connect_mod._real_ssh_connect("127.0.0.1", 2222) is False


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
    assert conn.closed == 1


def test_resolve_ssh_endpoint_without_a_recorded_port_is_configuration_error() -> None:
    # A System provisioned without ssh_credential_ref records no SSH forward — actionable, not a
    # missing dep.
    conn = _FakeGdbConn(domain=_FakeGdbDomain("<domain/>"))
    resolver = connect_mod._resolve_ssh_endpoint_via(lambda: conn)
    with pytest.raises(CategorizedError) as exc:
        resolver(_SYSTEM)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "ssh_credential_ref" in str(exc.value)


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
