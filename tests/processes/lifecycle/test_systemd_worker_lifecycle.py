"""Replay and evidence ordering for retained systemd worker slots."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable
from dataclasses import replace

import pytest
from pydantic import SecretStr

from kdive.processes.lifecycle.systemd_worker_contract import (
    LifecycleRequest,
    LifecycleResponse,
    SlotPhase,
    WorkerSettings,
)
from kdive.processes.lifecycle.systemd_worker_lifecycle import (
    EvidenceRejected,
    SystemdWorkerLifecycle,
)
from kdive.processes.lifecycle.systemd_worker_runtime import (
    BootObservation,
    CgroupMembership,
    CommandDeadlineExceeded,
    Deadline,
    MonotonicDeadline,
    SystemdConflict,
    SystemdUnavailable,
    UnitObservation,
    UnmanagedWorker,
)
from kdive.processes.lifecycle.systemd_worker_state import SlotState, TerminationOutcome

_BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"
_NEXT_BOOT_ID = "fedcba98-7654-3210-fedc-ba9876543210"


class FakeClock:
    """Deterministic monotonic clock and wait seam."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeStore:
    """Complete in-memory double for one fixed ``SlotStore``."""

    def __init__(
        self,
        slot: int,
        events: list[str],
        *,
        state: SlotState | None = None,
        released: bool | None = None,
    ) -> None:
        self.slot = slot
        self.unit = f"kdive-live-worker@{slot}.service"
        self.events = events
        self.state = state
        self.environment = state is not None
        self.credential = state is not None
        self.release = (
            state is not None and state.phase in {SlotPhase.STARTED, SlotPhase.TERMINATED}
            if released is None
            else released
        )
        self.preparations = 0
        self.load_calls = 0

    def prepare(self, settings: WorkerSettings | None) -> SlotState:
        assert settings is not None
        assert self.state is None
        self.preparations += 1
        generation = f"{self.slot:x}{self.preparations:x}".ljust(32, "0")
        credential = f"credential-{self.slot}-{self.preparations}".encode()
        credential_hash = hashlib.sha256(credential).hexdigest()
        self.state = _state(
            self.slot,
            SlotPhase.PREPARED,
            generation=generation,
            credential_hash=credential_hash,
        )
        self.environment = True
        self.credential = True
        self.release = False
        self.events.append("persist:prepared")
        return self.state

    def load(self) -> SlotState | None:
        self.load_calls += 1
        return self.state

    def persist(self, state: SlotState) -> None:
        assert self.state is not None
        assert state.generation == self.state.generation
        self.state = state
        self.events.append(f"persist:{state.phase.value}")

    def publish_release(self, state: SlotState) -> None:
        assert state == self.state
        assert state.phase is SlotPhase.REGISTERED
        self.release = True
        self.events.append("release:publish")

    def discard_prepared(self, state: SlotState) -> None:
        assert state == self.state
        assert state.phase is SlotPhase.PREPARED
        assert not self.release
        self.environment = False
        self.credential = False
        self.state = None
        self.events.append("state:discard-prepared")

    def cleanup_terminated(self, state: SlotState) -> None:
        assert state == self.state
        assert state.phase is SlotPhase.TERMINATED
        self.environment = False
        self.credential = False
        self.release = False
        self.state = None
        self.events.append("state:cleanup")


class FakeRuntime:
    """Stateful exact-invocation runtime with no real systemd or sleeps."""

    def __init__(self, events: list[str], clock: FakeClock) -> None:
        self.events = events
        self.clock = clock
        self.current: dict[str, UnitObservation | BootObservation] = {}
        self.unmanaged: tuple[UnmanagedWorker, ...] = ()
        self.start_failures: dict[str, Exception] = {}
        self.observe_failures: dict[str, Exception] = {}
        self.keep_populated: set[str] = set()
        self.advance_on_start = 0.0
        self.start_counts: dict[str, int] = {}
        self.signaled: list[str] = []
        self.stopped: list[str] = []
        self.resets: list[str] = []
        self.journal_calls: list[tuple[str, int, float]] = []
        self.stop_budgets: list[float] = []
        self.inactive_checks: list[tuple[str, float]] = []
        self.systemd_deadlines: list[tuple[str, Deadline]] = []

    def require_inactive(self, unit: str, deadline: Deadline) -> None:
        self.systemd_deadlines.append(("require-inactive", deadline))
        self.inactive_checks.append((unit, deadline.remaining()))
        if unit in self.current:
            raise SystemdConflict("fixed worker unit is not inactive and empty")

    def start(self, unit: str, deadline: Deadline) -> None:
        self.systemd_deadlines.append(("start", deadline))
        assert deadline.remaining() >= 0
        self.events.append(f"systemd:start:{unit}")
        if self.advance_on_start:
            self.clock.advance(self.advance_on_start)
        if failure := self.start_failures.get(unit):
            raise failure
        retained = self.current.get(unit)
        if isinstance(retained, BootObservation):
            retained = None
        if retained is None or retained.membership == "empty":
            count = self.start_counts.get(unit, 0) + 1
            self.start_counts[unit] = count
            invocation_id = f"{_slot_from_unit(unit):x}{count:x}".ljust(32, "0")
            self.current[unit] = _observation(
                _slot_from_unit(unit), "populated", invocation_id=invocation_id
            )

    def observe(self, unit: str, deadline: Deadline) -> UnitObservation | BootObservation:
        self.systemd_deadlines.append(("observe", deadline))
        assert deadline.remaining() >= 0
        if failure := self.observe_failures.get(unit):
            raise failure
        observation = self.current[unit]
        if (
            unit in self.signaled
            and isinstance(observation, UnitObservation)
            and observation.membership == "empty"
        ):
            self.events.append(f"systemd:observe-empty:{unit}")
        return observation

    def signal_terminate(self, unit: str, deadline: Deadline) -> None:
        self.systemd_deadlines.append(("signal-terminate", deadline))
        assert deadline.remaining() >= 0
        self.events.append(f"systemd:signal-terminate:{unit}")
        self.signaled.append(unit)
        if unit not in self.keep_populated:
            current = self.current[unit]
            assert isinstance(current, UnitObservation)
            self.current[unit] = replace(
                current,
                active_state="inactive",
                sub_state="dead",
                result="success",
                exec_main_status=0,
                membership="empty",
            )

    def stop_retained(self, unit: str, deadline: Deadline) -> None:
        self.systemd_deadlines.append(("stop-retained", deadline))
        self.stop_budgets.append(deadline.remaining())
        self.stopped.append(unit)
        self.events.append(f"systemd:stop:{unit}")

    def reset(self, unit: str, deadline: Deadline) -> None:
        self.systemd_deadlines.append(("reset", deadline))
        assert deadline.remaining() >= 0
        self.resets.append(unit)
        self.events.append(f"systemd:reset:{unit}")
        self.current.pop(unit, None)

    def unmanaged_workers(self) -> tuple[UnmanagedWorker, ...]:
        return self.unmanaged

    def journal(self, invocation_id: str, byte_limit: int, deadline: Deadline) -> str:
        self.journal_calls.append((invocation_id, byte_limit, deadline.remaining()))
        return "untrusted journal text"


class FakeAuthority:
    """Idempotent authority double with configurable dependency and evidence failures."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.registered: set[str] = set()
        self.register_label = "database:register"
        self.terminate_label = "database:terminate"
        self.fail_register = False
        self.reject_termination = False
        self.terminations: list[tuple[str, TerminationOutcome]] = []

    async def register(self, state: SlotState, credential_hash: bytes) -> None:
        assert credential_hash == bytes.fromhex(state.credential_hash)
        self.events.append(self.register_label)
        if self.fail_register:
            raise RuntimeError("database unavailable")
        self.registered.add(state.incarnation)

    async def terminate(self, state: SlotState, outcome: TerminationOutcome) -> None:
        self.events.append(self.terminate_label)
        if self.reject_termination:
            raise EvidenceRejected("database rejected exact evidence")
        assert state.incarnation in self.registered
        self.terminations.append((state.incarnation, outcome))


def _state(
    slot: int,
    phase: SlotPhase,
    *,
    generation: str | None = None,
    credential_hash: str = "c" * 64,
    boot_id: str = _BOOT_ID,
    invocation_id: str | None = None,
    outcome: TerminationOutcome | None = None,
) -> SlotState:
    unit = f"kdive-live-worker@{slot}.service"
    generation = generation or f"{slot:x}" * 32
    binding = phase is not SlotPhase.PREPARED
    return SlotState(
        schema=1,
        slot=slot,
        unit=unit,
        generation=generation,
        incarnation=f"local-systemd:{unit}:{generation}",
        credential_hash=credential_hash,
        phase=phase,
        boot_id=boot_id if binding else None,
        invocation_id=(invocation_id or f"{slot:x}" * 32) if binding else None,
        outcome=outcome,
    )


def _observation(
    slot: int,
    membership: CgroupMembership,
    *,
    boot_id: str = _BOOT_ID,
    invocation_id: str | None = None,
    result: str = "success",
    status: int = 0,
) -> UnitObservation:
    unit = f"kdive-live-worker@{slot}.service"
    return UnitObservation(
        unit=unit,
        boot_id=boot_id,
        invocation_id=invocation_id or f"{slot:x}" * 32,
        active_state="active" if membership == "populated" else "inactive",
        sub_state="running" if membership == "populated" else "dead",
        result=result,
        exec_main_status=status,
        control_group=f"/system.slice/{unit}",
        membership=membership,
    )


def _boot_observation(slot: int, *, boot_id: str = _BOOT_ID) -> BootObservation:
    return BootObservation(unit=f"kdive-live-worker@{slot}.service", boot_id=boot_id)


def _slot_from_unit(unit: str) -> int:
    return int(unit.removeprefix("kdive-live-worker@").removesuffix(".service"))


def _settings() -> WorkerSettings:
    return WorkerSettings(
        python="/usr/bin/python3.14",
        source_root="/src/kdive",
        rootfs_dir="/var/lib/kdive/rootfs",
        build_workspace="/var/lib/kdive/build",
        build_component_roots="/var/lib/kdive/components",
        install_staging="/var/lib/kdive/install",
        fixture_catalog_path="/etc/kdive/fixtures.toml",
        worker_database_url=SecretStr("postgresql://worker@localhost/kdive"),
        libvirt_uri="qemu:///session",
        s3_endpoint_url="http://127.0.0.1:9000",
        s3_bucket="kdive",
        s3_region="us-west-2",
        aws_access_key_id=SecretStr("access"),
        aws_secret_access_key=SecretStr("secret"),
        accepted_lanes=("default", "state-fenced"),
        build_user="builder",
        log_level="INFO",
        health_binds={1: "127.0.0.1:9101", 2: "127.0.0.1:9102"},
    )


def _request(worker_count: int = 1) -> LifecycleRequest:
    return LifecycleRequest(operation="start", worker_count=worker_count, settings=_settings())


def _deadline(clock: FakeClock, seconds: float = 1_000.0) -> MonotonicDeadline:
    return MonotonicDeadline.after(seconds, monotonic=clock)


def _coordinator(
    stores: list[FakeStore], runtime: FakeRuntime, authority: FakeAuthority, clock: FakeClock
) -> SystemdWorkerLifecycle:
    return SystemdWorkerLifecycle(
        stores=tuple(stores),
        runtime=runtime,
        authority=authority,
        wait=clock.advance,
    )


def _run(coroutine: Awaitable[LifecycleResponse]) -> LifecycleResponse:
    return asyncio.run(coroutine)


def _fleet(
    *, states: dict[int, SlotState] | None = None, releases: dict[int, bool] | None = None
) -> tuple[list[FakeStore], FakeRuntime, FakeAuthority, FakeClock, list[str]]:
    events: list[str] = []
    clock = FakeClock()
    state_by_slot = states or {}
    release_by_slot = releases or {}
    stores = [
        FakeStore(
            slot,
            events,
            state=state_by_slot.get(slot),
            released=release_by_slot.get(slot),
        )
        for slot in range(1, 9)
    ]
    runtime = FakeRuntime(events, clock)
    authority = FakeAuthority(events)
    for state in state_by_slot.values():
        if state.boot_id is not None and state.invocation_id is not None:
            runtime.current[state.unit] = _observation(
                state.slot,
                "populated",
                boot_id=state.boot_id,
                invocation_id=state.invocation_id,
            )
        if state.phase in {SlotPhase.REGISTERED, SlotPhase.STARTED, SlotPhase.TERMINATED}:
            authority.registered.add(state.incarnation)
    return stores, runtime, authority, clock, events


def test_start_mints_unique_generation_and_credential_per_slot() -> None:
    stores, runtime, authority, clock, _ = _fleet()
    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(2), _deadline(clock))
    )

    assert response.ok
    states = [stores[index].state for index in range(2)]
    assert all(state is not None and state.phase is SlotPhase.STARTED for state in states)
    assert len({state.generation for state in states if state is not None}) == 2
    assert len({state.credential_hash for state in states if state is not None}) == 2


def test_start_refuses_unmanaged_worker_without_mutating_slots() -> None:
    stores, runtime, authority, clock, events = _fleet()
    runtime.unmanaged = (UnmanagedWorker(pid=77, uid=1000),)

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert (response.code, response.retry_action) == ("conflict", "operator_recovery")
    assert all(store.state is None for store in stores)
    assert events == []


def test_start_refuses_populated_fixed_unit_without_retained_state() -> None:
    stores, runtime, authority, clock, events = _fleet()
    unit = "kdive-live-worker@1.service"
    runtime.current[unit] = _observation(1, "populated")

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert (response.code, response.retry_action) == ("conflict", "operator_recovery")
    assert stores[0].preparations == 0
    assert stores[0].state is None
    assert authority.registered == set() and authority.terminations == []
    assert not stores[0].release
    assert runtime.signaled == [] and runtime.stopped == [] and runtime.resets == []
    assert "state:cleanup" not in events


def test_start_reconciles_all_occupied_slots_before_replacement() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in (1, 2)}
    stores, runtime, authority, clock, events = _fleet(states=states)

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert response.ok
    first_prepare = events.index("persist:prepared")
    assert events[:first_prepare].count("state:cleanup") == 2
    first_signal = next(
        index
        for index, (operation, _) in enumerate(runtime.systemd_deadlines)
        if operation == "signal-terminate"
    )
    last_reset = max(
        index
        for index, (operation, _) in enumerate(runtime.systemd_deadlines)
        if operation == "reset"
    )
    assert (
        len(
            {
                id(deadline)
                for _, deadline in runtime.systemd_deadlines[first_signal : last_reset + 1]
            }
        )
        == 1
    )
    assert stores[0].state is not None and stores[0].state.phase is SlotPhase.STARTED
    assert stores[1].state is None


def test_start_adopts_prepared_generation_before_replacing_it() -> None:
    prepared = _state(1, SlotPhase.PREPARED)
    stores, runtime, authority, clock, events = _fleet(states={1: prepared})

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert response.ok
    assert prepared.incarnation in authority.registered
    assert events.index("systemd:start:kdive-live-worker@1.service") < events.index("state:cleanup")
    assert stores[0].state is not None
    assert stores[0].state.generation != prepared.generation


def test_start_adopts_exact_gated_invocation_before_replacing_it() -> None:
    gated = _state(1, SlotPhase.GATED)
    stores, runtime, authority, clock, events = _fleet(states={1: gated}, releases={1: False})

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert response.ok
    assert gated.incarnation in authority.registered
    assert events.index("database:register") < events.index("release:publish")
    assert events.index("release:publish") < events.index("state:cleanup")


def test_start_registers_before_release() -> None:
    stores, runtime, authority, clock, events = _fleet()

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert response.ok
    assert events == [
        "persist:prepared",
        "systemd:start:kdive-live-worker@1.service",
        "persist:gated",
        "database:register",
        "persist:registered",
        "release:publish",
        "persist:started",
    ]


@pytest.mark.parametrize("already_registered", [False, True])
def test_pre_release_gate_exit_replays_before_and_after_database_commit(
    already_registered: bool,
) -> None:
    gated = _state(1, SlotPhase.GATED)
    stores, runtime, authority, clock, events = _fleet(states={1: gated}, releases={1: False})
    runtime.current[gated.unit] = _observation(1, "empty")
    authority.registered.clear()
    if already_registered:
        authority.registered.add(gated.incarnation)
    authority.register_label = "database:register-same-generation"
    authority.terminate_label = "database:terminate-exact-empty-invocation"

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert response.ok
    assert events == [
        "database:register-same-generation",
        "persist:registered",
        "database:terminate-exact-empty-invocation",
        "persist:terminated",
        "systemd:stop:kdive-live-worker@1.service",
        "systemd:reset:kdive-live-worker@1.service",
        "state:cleanup",
    ]


def test_start_replays_database_commit_with_same_generation() -> None:
    gated = _state(1, SlotPhase.GATED)
    stores, runtime, authority, clock, _ = _fleet(states={1: gated}, releases={1: False})
    authority.registered.add(gated.incarnation)

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert response.ok
    assert [incarnation for incarnation in authority.registered if incarnation == gated.incarnation]
    assert authority.terminations[0][0] == gated.incarnation


def test_stale_same_boot_invocation_is_refused_without_signaling_or_cleanup() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "populated", invocation_id="f" * 32)

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert response.code == "conflict"
    assert runtime.signaled == []
    assert runtime.stopped == []
    assert stores[0].state == started
    assert stores[0].environment and stores[0].credential and stores[0].release


def test_partial_start_rolls_back_only_slots_activated_by_this_request() -> None:
    stores, runtime, authority, clock, _ = _fleet()
    second_unit = "kdive-live-worker@2.service"
    runtime.start_failures[second_unit] = SystemdUnavailable("system manager unavailable")

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(2), _deadline(clock))
    )

    assert (response.code, response.retry_action) == (
        "dependency_unavailable",
        "restore_systemd",
    )
    assert stores[0].state is None
    assert stores[1].state is not None and stores[1].state.phase is SlotPhase.PREPARED
    assert runtime.stopped == ["kdive-live-worker@1.service"]
    first_signal = next(
        index
        for index, (operation, _) in enumerate(runtime.systemd_deadlines)
        if operation == "signal-terminate"
    )
    assert len({id(deadline) for _, deadline in runtime.systemd_deadlines[first_signal:]}) == 1
    assert [result.slot for result in response.slots] == [1, 2]


def test_stop_discards_proven_inactive_prepared_generation() -> None:
    prepared = _state(1, SlotPhase.PREPARED)
    stores, runtime, authority, clock, events = _fleet(states={1: prepared})

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert response.ok
    assert events == ["state:discard-prepared"]
    assert stores[0].state is None
    assert not stores[0].environment and not stores[0].credential and not stores[0].release
    assert runtime.start_counts == {}
    assert runtime.signaled == [] and runtime.stopped == [] and runtime.resets == []
    assert authority.registered == set() and authority.terminations == []


def test_stop_adopts_active_prepared_gate_without_starting_another_invocation() -> None:
    prepared = _state(1, SlotPhase.PREPARED)
    stores, runtime, authority, clock, events = _fleet(states={1: prepared})
    runtime.current[prepared.unit] = _observation(1, "populated")

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert response.ok
    assert events == [
        "persist:gated",
        "systemd:signal-terminate:kdive-live-worker@1.service",
        "systemd:observe-empty:kdive-live-worker@1.service",
        "database:register",
        "persist:registered",
        "database:terminate",
        "persist:terminated",
        "systemd:stop:kdive-live-worker@1.service",
        "systemd:reset:kdive-live-worker@1.service",
        "state:cleanup",
    ]
    assert runtime.start_counts == {}
    assert prepared.incarnation in authority.registered
    assert authority.terminations == [(prepared.incarnation, "succeeded")]


def test_stop_retains_prepared_generation_when_invocation_facts_are_uncertain() -> None:
    prepared = _state(1, SlotPhase.PREPARED)
    stores, runtime, authority, clock, events = _fleet(states={1: prepared})
    runtime.current[prepared.unit] = _observation(1, "unknown")

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert (response.code, response.retry_action) == (
        "dependency_unavailable",
        "restore_systemd",
    )
    assert stores[0].state == prepared
    assert stores[0].environment and stores[0].credential and not stores[0].release
    assert runtime.start_counts == {}
    assert runtime.signaled == [] and runtime.stopped == [] and runtime.resets == []
    assert authority.registered == set() and authority.terminations == []
    assert "state:discard-prepared" not in events and "state:cleanup" not in events


def test_start_stops_slots_above_a_reduced_worker_count() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in (1, 2, 3)}
    stores, runtime, authority, clock, _ = _fleet(states=states)

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert response.ok
    assert stores[0].state is not None and stores[0].state.phase is SlotPhase.STARTED
    assert stores[1].state is None and stores[2].state is None
    assert set(runtime.stopped) >= {
        "kdive-live-worker@2.service",
        "kdive-live-worker@3.service",
    }


def test_same_boot_unit_absence_is_not_terminal_evidence() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.observe_failures[started.unit] = SystemdUnavailable("unit is absent")

    response = _run(_coordinator(stores, runtime, authority, clock).status(_deadline(clock)))

    assert (response.code, response.retry_action) == (
        "dependency_unavailable",
        "restore_systemd",
    )
    assert authority.terminations == []
    assert stores[0].state == started


def test_reboot_maps_exact_retained_binding_to_killed() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _boot_observation(1, boot_id=_NEXT_BOOT_ID)

    response = _run(_coordinator(stores, runtime, authority, clock).status(_deadline(clock)))

    assert response.ok
    assert authority.terminations == [(started.incarnation, "killed")]
    assert stores[0].state is not None
    assert stores[0].state.phase is SlotPhase.TERMINATED
    assert stores[0].state.outcome == "killed"


def test_same_boot_inactive_unit_is_not_terminal_evidence() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _boot_observation(1)

    response = _run(_coordinator(stores, runtime, authority, clock).status(_deadline(clock)))

    assert (response.code, response.retry_action) == (
        "dependency_unavailable",
        "restore_systemd",
    )
    assert authority.terminations == []
    assert stores[0].state == started


@pytest.mark.parametrize(
    ("result", "status", "outcome"),
    [("success", 0, "succeeded"), ("exit-code", 2, "failed"), ("signal", 15, "killed")],
)
def test_empty_invocation_result_maps_to_terminal_outcome(
    result: str, status: int, outcome: TerminationOutcome
) -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "empty", result=result, status=status)

    response = _run(_coordinator(stores, runtime, authority, clock).status(_deadline(clock)))

    assert response.ok
    assert authority.terminations == [(started.incarnation, outcome)]
    assert stores[0].state is not None and stores[0].state.outcome == outcome


def test_stop_commits_evidence_before_reset_and_cleanup() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: started})

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert response.ok
    assert events == [
        "systemd:signal-terminate:kdive-live-worker@1.service",
        "systemd:observe-empty:kdive-live-worker@1.service",
        "database:terminate",
        "persist:terminated",
        "systemd:stop:kdive-live-worker@1.service",
        "systemd:reset:kdive-live-worker@1.service",
        "state:cleanup",
    ]
    stop_path_deadlines = [
        deadline
        for operation, deadline in runtime.systemd_deadlines
        if operation in {"observe", "signal-terminate", "stop-retained", "reset"}
    ]
    assert len({id(deadline) for deadline in stop_path_deadlines}) == 1


def test_stop_signaling_and_observation_share_a_45_second_ceiling() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.keep_populated.add(started.unit)

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert response.code == "deadline_exceeded"
    assert clock.value == pytest.approx(45.0)
    assert authority.terminations == []
    assert runtime.stopped == [] and runtime.resets == []
    assert stores[0].state == started


def test_reserved_child_timeout_is_reported_as_deadline_exceeded() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.observe_failures[started.unit] = CommandDeadlineExceeded("command timed out")

    response = _run(_coordinator(stores, runtime, authority, clock).status(_deadline(clock)))

    assert (response.code, response.retry_action) == (
        "deadline_exceeded",
        "retry_same_operation",
    )
    assert authority.terminations == []
    assert stores[0].state == started


def test_start_replacement_shares_one_stop_ceiling_across_occupied_slots() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in (1, 2)}
    stores, runtime, authority, clock, _ = _fleet(states=states)
    runtime.keep_populated.update(state.unit for state in states.values())

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert response.code == "deadline_exceeded"
    assert clock.value == pytest.approx(45.0)
    assert runtime.signaled == [states[1].unit, states[2].unit]
    assert stores[0].state == states[1] and stores[1].state == states[2]
    assert authority.terminations == []


def test_start_uses_one_absolute_120_second_request_ceiling() -> None:
    stores, runtime, authority, clock, _ = _fleet()
    runtime.advance_on_start = 121.0

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert response.code == "deadline_exceeded"
    assert stores[0].state is not None and stores[0].state.phase is SlotPhase.PREPARED
    assert authority.registered == set()
    assert runtime.stopped == [] and runtime.resets == []
    assert sum(store.load_calls for store in stores) == 8


def test_failed_database_dependency_retains_generation_and_host_objects() -> None:
    stores, runtime, authority, clock, _ = _fleet()
    authority.fail_register = True

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert (response.code, response.retry_action) == (
        "dependency_unavailable",
        "restore_database",
    )
    assert stores[0].state is not None and stores[0].state.phase is SlotPhase.GATED
    assert stores[0].environment and stores[0].credential and not stores[0].release
    assert runtime.stopped == [] and runtime.resets == []


def test_evidence_rejection_retains_phase_and_every_host_object() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "empty", result="exit-code", status=1)
    authority.reject_termination = True

    response = _run(_coordinator(stores, runtime, authority, clock).status(_deadline(clock)))

    assert (response.code, response.retry_action) == (
        "evidence_rejected",
        "retry_same_operation",
    )
    assert stores[0].state == started
    assert stores[0].environment and stores[0].credential and stores[0].release
    assert runtime.stopped == [] and runtime.resets == []
    assert not any(event == "persist:terminated" for event in stores[0].events)
    assert not any(event == "state:cleanup" for event in stores[0].events)


def test_status_records_unexpected_exit_without_cleaning_diagnostic_sources() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "empty", result="exit-code", status=1)
    coordinator = _coordinator(stores, runtime, authority, clock)

    status = _run(coordinator.status(_deadline(clock)))

    assert status.ok
    terminated = stores[0].state
    assert terminated is not None and terminated.phase is SlotPhase.TERMINATED
    assert terminated.outcome == "failed"
    assert stores[0].environment and stores[0].credential and stores[0].release
    assert started.unit in runtime.current
    assert runtime.stopped == [] and runtime.resets == []
    assert events == ["database:terminate", "persist:terminated"]

    diagnostics = _run(coordinator.diagnostics(_deadline(clock)))

    assert diagnostics.code == "diagnostics_withheld"
    assert runtime.journal_calls == [(started.invocation_id, 320 * 1024, 30.0)]
    assert stores[0].state == terminated
    assert stores[0].environment and stores[0].credential and stores[0].release

    stopped = _run(coordinator.stop(_deadline(clock)))

    assert stopped.ok
    assert stores[0].state is None
    assert runtime.stopped == [started.unit]
    assert runtime.resets == [started.unit]


def test_diagnostics_uses_one_30_second_acquisition_ceiling_and_never_mutates() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in (1, 2)}
    stores, runtime, authority, clock, events = _fleet(states=states)
    coordinator = _coordinator(stores, runtime, authority, clock)

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.code == "diagnostics_withheld"
    assert [call[0] for call in runtime.journal_calls] == [
        states[1].invocation_id,
        states[2].invocation_id,
    ]
    assert all(budget <= 30.0 for _, _, budget in runtime.journal_calls)
    assert events == []
    assert stores[0].state == states[1] and stores[1].state == states[2]
