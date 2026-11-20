"""The fault-inject hot-plug port: device table, behaviours and injected faults (ADR-0823)."""

from __future__ import annotations

from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import PowerAction
from kdive.providers.assembly.composition import build_fault_inject_runtime
from kdive.providers.fault_inject.capabilities import HOTPLUG_BEHAVIOUR_KEY
from kdive.providers.fault_inject.faulting.engine import FaultEngine
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.fault_inject.lifecycle.faulted import FaultedHotplug
from kdive.providers.fault_inject.lifecycle.hotplug import FaultInjectHotplug, HotplugBehaviour
from kdive.providers.ports.lifecycle import DeviceHotplugger, DeviceSpec, HotplugDeviceKind

_SYSTEM = UUID("44444444-4444-4444-4444-444444444444")
_DOMAIN = f"fault-inject-{_SYSTEM}"
_NIC = DeviceSpec(HotplugDeviceKind.NIC, "dev-nic", network=None, size_gb=None)
_DISK = DeviceSpec(HotplugDeviceKind.DISK, "dev-disk", network=None, size_gb=4)


def _port(behaviour: HotplugBehaviour | None = None) -> FaultInjectHotplug:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, _DOMAIN)
    return FaultInjectHotplug(inventory, behaviour)


def _raises(category: ErrorCategory, reason: str) -> pytest.RaisesExc[CategorizedError]:
    return pytest.raises(
        CategorizedError,
        check=lambda err: err.category is category and err.details.get("reason") == reason,
    )


@pytest.mark.parametrize("spec", [_NIC, _DISK], ids=["nic", "disk"])
def test_plug_then_unplug_round_trips(spec: DeviceSpec) -> None:
    port = _port()

    outcome = port.plug(_DOMAIN, spec)

    assert outcome.settled is True
    assert (
        port.is_attached(_DOMAIN, spec.device_id, outcome.provider_handle, kind=spec.kind) is True
    )
    assert port.unplug(_DOMAIN, spec.device_id, outcome.provider_handle, kind=spec.kind) is True
    assert (
        port.is_attached(_DOMAIN, spec.device_id, outcome.provider_handle, kind=spec.kind) is False
    )


def test_identity_is_a_mac_for_a_nic_and_a_bounded_serial_for_a_disk() -> None:
    port = _port()

    nic = port.plug(_DOMAIN, _NIC).identity
    disk = port.plug(_DOMAIN, _DISK).identity

    assert set(nic) == {"mac"}
    assert nic["mac"].startswith("52:54:00:") and len(nic["mac"]) == 17
    assert set(disk) == {"serial"}
    assert 0 < len(disk["serial"]) <= 20
    assert port.plug(_DOMAIN, _NIC).identity == nic


def test_plug_is_idempotent_on_device_id() -> None:
    port = _port()

    first = port.plug(_DOMAIN, _NIC)

    assert port.plug(_DOMAIN, _NIC) == first


def test_unplug_of_an_absent_device_with_a_handle_succeeds() -> None:
    port = _port()
    handle = port.plug(_DOMAIN, _NIC).provider_handle
    assert port.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True

    assert port.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True


def test_unplug_of_an_absent_device_without_a_handle_is_handle_unknown() -> None:
    with _raises(ErrorCategory.CONTROL_FAILURE, "handle_unknown"):
        _port().unplug(_DOMAIN, "never-plugged", None, kind=HotplugDeviceKind.NIC)


def test_unplug_finds_a_present_device_without_a_handle() -> None:
    port = _port()
    port.plug(_DOMAIN, _DISK)

    assert port.unplug(_DOMAIN, _DISK.device_id, None, kind=HotplugDeviceKind.DISK) is True


def test_unplug_with_a_foreign_handle_is_handle_unknown() -> None:
    port = _port()
    port.plug(_DOMAIN, _NIC)

    with _raises(ErrorCategory.CONTROL_FAILURE, "handle_unknown"):
        port.unplug(_DOMAIN, _NIC.device_id, "another-handle", kind=HotplugDeviceKind.NIC)


def test_busy_refuses_unplug_and_keeps_the_device() -> None:
    port = _port(HotplugBehaviour.BUSY)
    handle = port.plug(_DOMAIN, _NIC).provider_handle

    with _raises(ErrorCategory.CONTROL_FAILURE, "unplug_refused"):
        port.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC)

    assert port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True


def test_slow_plug_settles_on_the_third_poll() -> None:
    port = _port(HotplugBehaviour.SLOW)

    outcome = port.plug(_DOMAIN, _NIC)
    polls = [
        port.is_attached(
            _DOMAIN, _NIC.device_id, outcome.provider_handle, kind=HotplugDeviceKind.NIC
        )
        for _ in range(5)
    ]

    assert outcome.settled is False
    assert polls == [False, False, True, True, True]


def test_slow_unplug_is_gone_on_the_third_poll() -> None:
    port = _port(HotplugBehaviour.SLOW)
    handle = port.plug(_DOMAIN, _NIC).provider_handle
    while not port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC):
        pass

    assert port.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is False
    assert port.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is False
    polls = [
        port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC)
        for _ in range(4)
    ]

    assert polls == [True, True, False, False]
    assert port.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True


def test_plug_of_a_device_being_removed_is_not_settled() -> None:
    port = _port(HotplugBehaviour.SLOW)
    handle = port.plug(_DOMAIN, _NIC).provider_handle
    while not port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC):
        pass
    port.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC)

    assert port.plug(_DOMAIN, _NIC).settled is False


def test_hang_plug_never_settles() -> None:
    port = _port(HotplugBehaviour.HANG)

    outcome = port.plug(_DOMAIN, _DISK)

    assert outcome.settled is False
    assert not any(
        port.is_attached(
            _DOMAIN, _DISK.device_id, outcome.provider_handle, kind=HotplugDeviceKind.DISK
        )
        for _ in range(50)
    )


def test_hang_unplug_never_settles() -> None:
    port = _port(HotplugBehaviour.HANG)
    handle = port.plug(_DOMAIN, _DISK).provider_handle
    # A hung plug is not attached yet; the removal still reports the device present, forever.
    port.unplug(_DOMAIN, _DISK.device_id, handle, kind=HotplugDeviceKind.DISK)

    assert all(
        port.is_attached(_DOMAIN, _DISK.device_id, handle, kind=HotplugDeviceKind.DISK)
        for _ in range(50)
    )
    assert port.unplug(_DOMAIN, _DISK.device_id, handle, kind=HotplugDeviceKind.DISK) is False


def test_plug_into_a_stopped_domain_is_domain_not_running() -> None:
    port = _port()
    port.note_power(_DOMAIN, PowerAction.OFF)

    with _raises(ErrorCategory.INFRASTRUCTURE_FAILURE, "domain_not_running"):
        port.plug(_DOMAIN, _NIC)


def test_power_off_drops_devices_and_unplug_still_succeeds_with_a_handle() -> None:
    port = _port()
    handle = port.plug(_DOMAIN, _DISK).provider_handle

    port.note_power(_DOMAIN, PowerAction.OFF)

    assert port.is_attached(_DOMAIN, _DISK.device_id, handle, kind=HotplugDeviceKind.DISK) is False
    assert port.unplug(_DOMAIN, _DISK.device_id, handle, kind=HotplugDeviceKind.DISK) is True


def test_power_on_after_off_accepts_a_plug_and_cycle_drops_devices() -> None:
    port = _port()
    port.note_power(_DOMAIN, PowerAction.OFF)
    port.note_power(_DOMAIN, PowerAction.ON)
    handle = port.plug(_DOMAIN, _NIC).provider_handle

    port.note_power(_DOMAIN, PowerAction.CYCLE)

    assert port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is False
    assert port.plug(_DOMAIN, _NIC).settled is True


@pytest.mark.parametrize("action", [PowerAction.RESET, PowerAction.RESUME])
def test_reset_and_resume_keep_devices(action: PowerAction) -> None:
    port = _port()
    handle = port.plug(_DOMAIN, _NIC).provider_handle

    port.note_power(_DOMAIN, action)

    assert port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True


def test_release_all_clears_the_domain_and_is_idempotent() -> None:
    port = _port()
    handle = port.plug(_DOMAIN, _NIC).provider_handle
    port.plug(_DOMAIN, _DISK)

    port.release_all(_DOMAIN)
    port.release_all(_DOMAIN)

    assert port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is False
    assert port.is_attached(_DOMAIN, _DISK.device_id, None, kind=HotplugDeviceKind.DISK) is False


def test_release_all_keeps_a_stopped_domain_stopped() -> None:
    port = _port()
    port.note_power(_DOMAIN, PowerAction.OFF)

    port.release_all(_DOMAIN)

    with _raises(ErrorCategory.INFRASTRUCTURE_FAILURE, "domain_not_running"):
        port.plug(_DOMAIN, _NIC)


def test_an_undefined_domain_is_an_infrastructure_failure() -> None:
    with _raises(ErrorCategory.INFRASTRUCTURE_FAILURE, "domain_absent"):
        _port().plug("fault-inject-unknown", _NIC)


def test_a_kind_outside_the_advertised_set_is_a_configuration_error() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, _DOMAIN)
    port = FaultInjectHotplug(inventory, kinds=frozenset({HotplugDeviceKind.NIC}))

    with pytest.raises(CategorizedError) as err:
        port.plug(_DOMAIN, _DISK)

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_devices_are_per_domain() -> None:
    inventory = FaultInjectInventory()
    other_system = UUID("55555555-5555-5555-5555-555555555555")
    other = f"fault-inject-{other_system}"
    inventory.record(_SYSTEM, _DOMAIN)
    inventory.record(other_system, other)
    port = FaultInjectHotplug(inventory)

    port.plug(_DOMAIN, _NIC)

    assert port.is_attached(other, _NIC.device_id, None, kind=HotplugDeviceKind.NIC) is False


# --- behaviour parsing ------------------------------------------------------------------


@pytest.mark.parametrize("behaviour", list(HotplugBehaviour))
def test_behaviour_is_read_from_the_capability_key(behaviour: HotplugBehaviour) -> None:
    assert HotplugBehaviour.from_capabilities({HOTPLUG_BEHAVIOUR_KEY: behaviour.value}) is behaviour


def test_an_absent_behaviour_settles_at_once() -> None:
    assert HotplugBehaviour.from_capabilities({}) is None


def test_an_unknown_behaviour_is_a_configuration_error() -> None:
    with pytest.raises(CategorizedError) as err:
        HotplugBehaviour.from_capabilities({HOTPLUG_BEHAVIOUR_KEY: "flaky"})

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- injected faults --------------------------------------------------------------------


def _faulted(
    rate: float, sleeps: list[float] | None = None
) -> tuple[FaultedHotplug, DeviceHotplugger]:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, _DOMAIN)
    engine = FaultEngine(seed=1, fault_rate={"hotplug": rate}, max_latency_s={})
    inner = FaultInjectHotplug(inventory)
    faulted = FaultedHotplug(inner, engine, inventory, sleep_s=(sleeps or []).append)
    return faulted, inner


def test_a_certain_fault_fails_plug_and_unplug_as_infrastructure_failure() -> None:
    faulted, inner = _faulted(1.0)

    for call in (
        lambda: faulted.plug(_DOMAIN, _NIC),
        lambda: faulted.unplug(_DOMAIN, _NIC.device_id, "handle", kind=HotplugDeviceKind.NIC),
    ):
        with pytest.raises(CategorizedError) as err:
            call()
        assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert inner.is_attached(_DOMAIN, _NIC.device_id, None, kind=HotplugDeviceKind.NIC) is False


def test_a_zero_rate_delegates_and_polling_is_never_perturbed() -> None:
    faulted, _ = _faulted(0.0)

    outcome = faulted.plug(_DOMAIN, _NIC)

    assert (
        faulted.is_attached(
            _DOMAIN, _NIC.device_id, outcome.provider_handle, kind=HotplugDeviceKind.NIC
        )
        is True
    )
    assert (
        faulted.unplug(_DOMAIN, _NIC.device_id, outcome.provider_handle, kind=HotplugDeviceKind.NIC)
        is True
    )


def test_release_all_is_never_perturbed_by_a_fault() -> None:
    faulted, inner = _faulted(1.0)
    inner.plug(_DOMAIN, _NIC)

    faulted.release_all(_DOMAIN)

    assert inner.is_attached(_DOMAIN, _NIC.device_id, None, kind=HotplugDeviceKind.NIC) is False


def test_an_undefined_domain_is_delegated_without_a_draw() -> None:
    faulted, _ = _faulted(1.0)

    with _raises(ErrorCategory.INFRASTRUCTURE_FAILURE, "domain_absent"):
        faulted.plug("fault-inject-unknown", _NIC)


def test_the_fault_is_deterministic_for_one_system() -> None:
    outcomes = []
    for _ in range(2):
        faulted, _ = _faulted(0.5)
        try:
            faulted.plug(_DOMAIN, _NIC)
            outcomes.append("ok")
        except CategorizedError:
            outcomes.append("fault")

    assert outcomes[0] == outcomes[1]


def _plug_outcomes(faulted: FaultedHotplug, calls: int) -> list[str]:
    outcomes = []
    for _ in range(calls):
        try:
            faulted.plug(_DOMAIN, _NIC)
            outcomes.append("ok")
        except CategorizedError:
            outcomes.append("fault")
    return outcomes


def test_successive_draws_on_one_system_differ_and_replay_from_the_seed() -> None:
    first = _plug_outcomes(_faulted(0.5)[0], 12)
    second = _plug_outcomes(_faulted(0.5)[0], 12)

    assert {"ok", "fault"} <= set(first)
    assert first == second


def test_reprovisioning_a_domain_restarts_its_draw_sequence() -> None:
    faulted, _ = _faulted(0.5)
    inventory = faulted._inventory  # noqa: SLF001 - the shared ordinal ledger under test
    first = _plug_outcomes(faulted, 8)

    inventory.forget(_DOMAIN)
    inventory.record(_SYSTEM, _DOMAIN)

    assert _plug_outcomes(faulted, 8) == first


def test_rebound_ports_share_one_draw_sequence_per_system() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, _DOMAIN)
    runtime = build_fault_inject_runtime(inventory=inventory)
    caps = {"fault_rate": {"hotplug": 0.5}, "seed": 1}
    rebinding = [runtime.for_resource("row", caps) for _ in range(12)]
    single = _faulted(0.5)[0]

    outcomes = []
    for bound in rebinding:
        assert isinstance(bound.device_hotplug, FaultedHotplug)
        outcomes.extend(_plug_outcomes(bound.device_hotplug, 1))

    assert outcomes == _plug_outcomes(single, 12)


# --- composition ------------------------------------------------------------------------


def test_runtime_wires_the_behaviour_and_follows_control_power() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, _DOMAIN)
    runtime = build_fault_inject_runtime(
        inventory=inventory, hotplug_behaviour=HotplugBehaviour.BUSY
    )
    port = runtime.device_hotplug
    assert port is not None
    handle = port.plug(_DOMAIN, _NIC).provider_handle

    with _raises(ErrorCategory.CONTROL_FAILURE, "unplug_refused"):
        port.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC)
    runtime.controller.power(_DOMAIN, PowerAction.OFF)

    assert port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is False
    with _raises(ErrorCategory.INFRASTRUCTURE_FAILURE, "domain_not_running"):
        port.plug(_DOMAIN, _NIC)


def test_runtime_with_an_engine_wraps_the_port_in_the_faulting_decorator() -> None:
    engine = FaultEngine(seed=0, fault_rate={}, max_latency_s={})

    runtime = build_fault_inject_runtime(engine=engine)

    assert isinstance(runtime.device_hotplug, FaultedHotplug)


# --- force_unplug (ADR-0828) -----------------------------------------------------------------


def _gone(port: FaultInjectHotplug, spec: DeviceSpec, handle: str | None) -> bool:
    return port.is_attached(_DOMAIN, spec.device_id, handle, kind=spec.kind) is False


@pytest.mark.parametrize("spec", [_NIC, _DISK], ids=["nic", "disk"])
@pytest.mark.parametrize(
    "behaviour",
    [None, HotplugBehaviour.BUSY, HotplugBehaviour.SLOW, HotplugBehaviour.HANG],
)
def test_force_unplug_removes_the_device_at_once_whatever_the_behaviour(
    spec: DeviceSpec, behaviour: HotplugBehaviour | None
) -> None:
    port = _port(behaviour)
    handle = port.plug(_DOMAIN, spec).provider_handle

    assert port.force_unplug(_DOMAIN, spec.device_id, handle) is True
    assert _gone(port, spec, handle)


def test_force_unplug_removes_a_device_that_is_removing_or_settling() -> None:
    port = _port()
    handle = port.plug(_DOMAIN, _NIC).provider_handle
    hung = port.with_behaviour(HotplugBehaviour.HANG)
    assert hung.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is False
    assert port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True

    assert port.force_unplug(_DOMAIN, _NIC.device_id, handle) is True
    assert _gone(port, _NIC, handle)


def test_force_unplug_of_an_absent_device_is_idempotent() -> None:
    port = _port()
    handle = port.plug(_DOMAIN, _NIC).provider_handle
    port.force_unplug(_DOMAIN, _NIC.device_id, handle)

    assert port.force_unplug(_DOMAIN, _NIC.device_id, handle) is True


def test_force_unplug_with_a_foreign_handle_is_handle_unknown_and_keeps_the_device() -> None:
    port = _port()
    handle = port.plug(_DOMAIN, _NIC).provider_handle

    with _raises(ErrorCategory.CONTROL_FAILURE, "handle_unknown"):
        port.force_unplug(_DOMAIN, _NIC.device_id, "another-handle")

    assert port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True


def test_force_unplug_of_an_absent_device_without_a_handle_is_handle_unknown() -> None:
    with _raises(ErrorCategory.CONTROL_FAILURE, "handle_unknown"):
        _port().force_unplug(_DOMAIN, "never-plugged", None)


def test_no_force_refuses_with_the_forced_flag_and_keeps_the_device() -> None:
    port = _port(HotplugBehaviour.NO_FORCE)
    handle = port.plug(_DOMAIN, _NIC).provider_handle

    with pytest.raises(CategorizedError) as excinfo:
        port.force_unplug(_DOMAIN, _NIC.device_id, handle)

    assert excinfo.value.category is ErrorCategory.CONTROL_FAILURE
    assert excinfo.value.details == {"reason": "unplug_refused", "forced": True}
    assert port.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True


def test_no_force_leaves_the_graceful_unplug_working() -> None:
    port = _port(HotplugBehaviour.NO_FORCE)
    handle = port.plug(_DOMAIN, _NIC).provider_handle

    assert port.unplug(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True


def test_an_injected_fault_fails_force_unplug_and_leaves_the_device() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, _DOMAIN)
    inner = FaultInjectHotplug(inventory)
    handle = inner.plug(_DOMAIN, _NIC).provider_handle
    engine = FaultEngine(seed=1, fault_rate={"hotplug": 1.0}, max_latency_s={})

    with pytest.raises(CategorizedError) as excinfo:
        FaultedHotplug(inner, engine, inventory).force_unplug(_DOMAIN, _NIC.device_id, handle)

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert inner.is_attached(_DOMAIN, _NIC.device_id, handle, kind=HotplugDeviceKind.NIC) is True


def test_the_no_force_capability_value_parses() -> None:
    assert (
        HotplugBehaviour.from_capabilities({HOTPLUG_BEHAVIOUR_KEY: "no_force"})
        is HotplugBehaviour.NO_FORCE
    )
