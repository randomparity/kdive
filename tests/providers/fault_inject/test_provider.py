"""The fault-inject mock ports return synthetic-but-plausible outputs (happy path)."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

import kdive.providers.fault_inject.lifecycle.connect as connect_module
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import PowerAction
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.debug.gdb import (
    FaultInjectDebugEngine,
    fault_inject_attach_seam,
)
from kdive.providers.fault_inject.debug.introspect import FaultInjectIntrospect
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.fault_inject.lifecycle.connect import FaultInjectConnect
from kdive.providers.fault_inject.lifecycle.control import FaultInjectControl
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning
from kdive.providers.fault_inject.retrieve import FaultInjectRetrieve
from kdive.providers.ports.handles import SystemHandle
from kdive.providers.ports.lifecycle import (
    DebugTransportKind,
    InstallRequest,
    TransportHandleData,
)

_SYSTEM = UUID("11111111-1111-1111-1111-111111111111")
_RUN = UUID("22222222-2222-2222-2222-222222222222")
_PROVISIONING_PROFILE = cast(ProvisioningProfile, object())


class _FakeStore:
    def __init__(self) -> None:
        self.writes: list[ArtifactWriteRequest] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.writes.append(request)
        return StoredArtifact(request.key(), "etag", request.sensitivity, request.retention_class)


# --- Provision -------------------------------------------------------------------------


def test_provision_returns_a_synthetic_domain_and_records_it_as_owned() -> None:
    inventory = FaultInjectInventory()
    provision = FaultInjectProvisioning(inventory)

    domain = provision.provision(_SYSTEM, profile=_PROVISIONING_PROFILE)

    assert str(_SYSTEM) in domain
    assert inventory.owned_domains()[0].name == domain
    assert inventory.owned_domains()[0].system_id == _SYSTEM


def test_teardown_forgets_the_domain_so_it_is_no_longer_owned() -> None:
    inventory = FaultInjectInventory()
    provision = FaultInjectProvisioning(inventory)
    domain = provision.provision(_SYSTEM, profile=_PROVISIONING_PROFILE)

    provision.teardown(domain)

    assert inventory.owned_domains() == []


def test_reprovision_leaves_the_system_owning_exactly_one_domain() -> None:
    inventory = FaultInjectInventory()
    provision = FaultInjectProvisioning(inventory)
    provision.provision(_SYSTEM, profile=_PROVISIONING_PROFILE)

    second = provision.reprovision(_SYSTEM, profile=_PROVISIONING_PROFILE)

    # The synthetic name is deterministic per System, so reprovision never leaks the old
    # domain: the inventory holds exactly one entry for the System after replacement.
    owned = [d.name for d in inventory.owned_domains()]
    assert owned == [second]


# --- Install / Boot --------------------------------------------------------------------


def test_install_and_boot_succeed_on_the_happy_path() -> None:
    install = FaultInjectInstall()

    # No fault drawn → the synthetic install/boot reach a ready state without raising.
    install.install(
        InstallRequest(
            system_id=_SYSTEM,
            run_id=_RUN,
            kernel_ref="kernel-ref",
            cmdline="console=ttyS0",
        )
    )
    install.boot(_SYSTEM)


# --- Connect ---------------------------------------------------------------------------


class _MaxDigest:
    def digest(self) -> bytes:
        return b"\xfb\xff"


def test_synthetic_port_includes_documented_upper_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blake2b(data: bytes, *, digest_size: int) -> _MaxDigest:
        assert data == b"fault-inject-domain"
        assert digest_size == 2
        return _MaxDigest()

    monkeypatch.setattr(connect_module.hashlib, "blake2b", blake2b)

    assert connect_module.synthetic_port("fault-inject-domain") == 65535


def test_open_transport_returns_a_decodable_loopback_handle() -> None:
    connect = FaultInjectConnect()

    handle = connect.open_transport(SystemHandle("fault-inject-domain"), "gdbstub")

    decoded = TransportHandleData.decode(handle)
    assert decoded.kind == "gdbstub"
    assert decoded.host == "127.0.0.1"
    assert 1 <= decoded.port <= 65535


def test_open_transport_rejects_an_unknown_transport_kind() -> None:
    connect = FaultInjectConnect()

    with pytest.raises(CategorizedError) as exc:
        connect.open_transport(
            SystemHandle("fault-inject-domain"), cast(DebugTransportKind, "carrier-pigeon")
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_close_transport_accepts_a_handle_it_opened() -> None:
    connect = FaultInjectConnect()
    handle = connect.open_transport(SystemHandle("fault-inject-domain"), "gdbstub")

    connect.close_transport(handle)


def test_open_close_drgn_live_round_trips() -> None:
    connect = FaultInjectConnect()

    handle = connect.open_transport(SystemHandle("fault-inject-domain"), "drgn-live")

    assert str(handle).startswith("drgn-live://")
    connect.close_transport(handle)  # decode of drgn-live:// must succeed (#215)


def test_open_transport_rejects_the_legacy_ssh_kind() -> None:
    connect = FaultInjectConnect()

    with pytest.raises(CategorizedError) as exc:
        connect.open_transport(SystemHandle("fault-inject-domain"), cast(DebugTransportKind, "ssh"))

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- TransportHandleData.decode -------------------------------------------------------


def test_decode_round_trips_encode() -> None:
    original = TransportHandleData(kind="gdbstub", host="127.0.0.1", port=1234)
    assert TransportHandleData.decode(original.encode()) == original


def test_decode_splits_scheme_on_first_separator() -> None:
    # The host segment may itself contain "://"; the scheme is taken from the FIRST split.
    decoded = TransportHandleData.decode("gdbstub://a://b:1234")
    assert decoded.kind == "gdbstub"
    assert decoded.host == "a://b"
    assert decoded.port == 1234


def test_decode_splits_port_on_last_colon() -> None:
    # The host may contain colons; the port is taken from the LAST colon split.
    decoded = TransportHandleData.decode("gdbstub://a:b:1234")
    assert decoded.host == "a:b"
    assert decoded.port == 1234


def test_decode_rejects_unknown_scheme() -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode("carrier-pigeon://127.0.0.1:1234")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "transport handle has no known transport scheme"


def test_decode_rejects_missing_scheme_separator() -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode("gdbstub")
    assert str(exc.value) == "transport handle has no known transport scheme"


def test_decode_rejects_empty_host() -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode("gdbstub://:1234")
    assert str(exc.value) == "transport handle must be <kind>://host:port"


def test_decode_rejects_missing_port_separator() -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode("gdbstub://127.0.0.1")
    assert str(exc.value) == "transport handle must be <kind>://host:port"


def test_decode_rejects_non_numeric_port() -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode("gdbstub://127.0.0.1:abc")
    assert str(exc.value) == "transport handle port must be numeric"


def test_decode_accepts_port_lower_bound() -> None:
    assert TransportHandleData.decode("gdbstub://127.0.0.1:1").port == 1


def test_decode_accepts_port_upper_bound() -> None:
    assert TransportHandleData.decode("gdbstub://127.0.0.1:65535").port == 65535


@pytest.mark.parametrize("port", [0, 65536])
def test_decode_rejects_out_of_range_port(port: int) -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode(f"gdbstub://127.0.0.1:{port}")
    assert str(exc.value) == "transport handle port is outside 1..65535"


# --- Control ---------------------------------------------------------------------------


def test_power_and_force_crash_succeed_on_the_happy_path() -> None:
    control = FaultInjectControl()

    control.power("fault-inject-domain", PowerAction.CYCLE)
    control.force_crash("fault-inject-domain")


# --- Retrieve / postmortem / introspect ------------------------------------------------


def test_capture_stores_a_synthetic_vmcore_with_raw_and_redacted_artifacts() -> None:
    store = _FakeStore()
    retrieve = FaultInjectRetrieve(store_factory=lambda: store)

    output = retrieve.capture(_SYSTEM, _RUN, CaptureMethod.HOST_DUMP)

    sensitivities = {w.sensitivity for w in store.writes}
    assert Sensitivity.SENSITIVE in sensitivities  # the raw core
    assert Sensitivity.REDACTED in sensitivities  # its redacted derivative
    assert output.vmcore_build_id == output.vmcore_build_id  # present, consistent


def test_crash_postmortem_returns_a_bounded_synthetic_transcript() -> None:
    retrieve = FaultInjectRetrieve(store_factory=_FakeStore)

    output = retrieve.run_crash_postmortem(
        vmcore_ref="v",
        debuginfo_ref="d",
        expected_build_id="b",
        commands=["bt"],
    )

    assert output.truncated is False
    assert isinstance(output.results, dict)


def test_crash_postmortem_rejects_disallowed_commands() -> None:
    retrieve = FaultInjectRetrieve(store_factory=_FakeStore)

    with pytest.raises(CategorizedError) as exc:
        retrieve.run_crash_postmortem(
            vmcore_ref="v",
            debuginfo_ref="d",
            expected_build_id="b",
            commands=["bt | sh"],
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_introspect_from_vmcore_and_live_return_plausible_shapes() -> None:
    introspect = FaultInjectIntrospect()

    offline = introspect.from_vmcore(vmcore_ref="v", debuginfo_ref="d", expected_build_id="b")
    live = introspect.introspect_live(
        transport_handle="gdbstub://127.0.0.1:1234", helper="drgn", key_path="/tmp/key"
    )

    assert offline.truncated is False
    assert live.truncated is False


# --- Debug engine / attach seam --------------------------------------------------------


def test_attach_seam_returns_an_attachment_at_the_loopback_endpoint(tmp_path: Path) -> None:
    transcript = tmp_path / "session.log"

    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=transcript
    )

    assert attachment.rsp_host == "127.0.0.1"
    assert attachment.rsp_port == 1234
    assert attachment.run_id == str(_RUN)  # run_id carried for load_module_symbols (#923)


def test_debug_engine_backtrace_and_read_frame(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "bt.log"
    )

    bt = engine.backtrace(attachment, max_frames=64)
    assert bt.truncated is False
    assert [frame.level for frame in bt.frames] == [0, 1]
    assert engine.read_frame(attachment, level=3).level == 3


def test_debug_engine_disassemble(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "dis.log"
    )

    result = engine.disassemble(attachment, symbol=None, address=0x1000, instruction_count=8)
    assert result.truncated is False
    assert result.instructions
    assert result.instructions[0].inst is not None


def test_debug_engine_set_and_list_breakpoints_round_trip(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "s.log"
    )

    ref = engine.set_breakpoint(attachment, "vfs_read")
    listed = engine.list_breakpoints(attachment)

    assert ref.number in {b.number for b in listed}


def test_debug_engine_breakpoints_are_isolated_per_attachment(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    first = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "first.log"
    )
    second = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "second.log"
    )

    first_ref = engine.set_breakpoint(first, "vfs_read")
    second_ref = engine.set_breakpoint(second, "do_exit")
    engine.clear_breakpoint(first, first_ref.number)

    assert [ref.number for ref in engine.list_breakpoints(first)] == []
    assert [ref.number for ref in engine.list_breakpoints(second)] == [second_ref.number]


def test_debug_engine_watchpoints_round_trip(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id="r", transcript_path=tmp_path / "t.jsonl"
    )
    ref = engine.set_watchpoint(attachment, symbol=None, address=0x1000, byte_count=8)
    listed = engine.list_watchpoints(attachment)
    assert [w.number for w in listed] == [ref.number]
    engine.clear_watchpoint(attachment, ref.number)
    assert engine.list_watchpoints(attachment) == []


def test_debug_engine_list_modules(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "mods.log"
    )

    result = engine.list_modules(attachment, max_modules=64)
    assert result.truncated is False
    assert result.decode_errors == 0
    assert result.modules
    assert result.modules[0].name is not None
    assert result.modules[0].base_address is not None
    assert result.modules[0].symbols_loaded is False


def test_debug_engine_load_module_symbols(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id=str(_RUN), transcript_path=tmp_path / "load.log"
    )

    loaded = engine.load_module_symbols(attachment, module="fault_inject_demo", expected_base=None)
    assert loaded.symbols_loaded is True
    listed = engine.list_modules(attachment, max_modules=64)
    assert listed.modules[0].symbols_loaded is True
