"""Connect-plane provider tests — RSP framing + the seam-injected Connector (no live_vm).

The RSP-framing codec and the `Connector` orchestration (loopback check, prober dispatch,
error mapping, handle codec) are covered with fakes; the real socket / libvirt-domain
endpoint paths are `live_vm`-gated seams exercised only under the gate.
"""

from __future__ import annotations

from typing import cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle import connect as connect_mod
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.ports import DebugTransportKind, SystemHandle, TransportHandleData
from kdive.providers.shared.debug_common import rsp as rsp_mod
from kdive.providers.shared.debug_common.rsp import rsp_frame, valid_rsp_frame

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


def test_from_env_resolver_raises_missing_dependency() -> None:
    connector = LocalLibvirtConnect.from_env()
    with pytest.raises(CategorizedError) as exc:
        connector.open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert str(exc.value) == (
        "resolving a libvirt domain's gdbstub endpoint runs only under the live_vm gate"
    )
    assert exc.value.details == {"system": str(_SYSTEM)}


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


def test_from_env_ssh_resolver_raises_missing_dependency() -> None:
    connector = LocalLibvirtConnect.from_env()
    with pytest.raises(CategorizedError) as exc:
        connector.open_transport(_SYSTEM, "drgn-live")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert str(exc.value) == (
        "resolving a libvirt guest's loopback-forwarded ssh endpoint runs only under "
        "the live_vm gate"
    )
    assert exc.value.details == {"system": str(_SYSTEM)}


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
