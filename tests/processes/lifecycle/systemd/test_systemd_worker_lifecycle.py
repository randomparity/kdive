"""Replay and evidence ordering for retained systemd worker slots (ADR-0574, ADR-0657)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import socket
from collections.abc import Awaitable, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr

import kdive.config as config_registry
import kdive.processes.lifecycle.systemd.systemd_diagnostics as diagnostics_module
import kdive.processes.lifecycle.systemd.systemd_worker_lifecycle as lifecycle
import kdive.processes.lifecycle.systemd.systemd_worker_runtime as runtime_module
import kdive.processes.lifecycle.systemd.systemd_worker_state as state_module
from kdive.domain.catalog.resources import ResourceKind
from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
    LifecycleRequest,
    LifecycleResponse,
    SlotPhase,
    WorkerSettings,
)
from kdive.processes.lifecycle.systemd.systemd_worker_lifecycle import (
    EvidenceRejected,
    LifecycleConflict,
    SystemdWorkerLifecycle,
)
from kdive.processes.lifecycle.systemd.systemd_worker_runtime import (
    BootObservation,
    CgroupMembership,
    CommandDeadlineExceeded,
    Deadline,
    MonotonicDeadline,
    SystemdConflict,
    SystemdRuntime,
    SystemdUnavailable,
    UnitObservation,
    UnmanagedWorker,
    load_slot_redaction_values,
)
from kdive.processes.lifecycle.systemd.systemd_worker_state import (
    SlotState,
    SlotStore,
    StateConflict,
)
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.local_libvirt.composition import build_runtime
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.external_boot.routing import (
    AuthorityReservationGeometry,
    authority_reservation_geometry,
)
from kdive.worker_lifecycle.authority_store import (
    IncarnationConflict,
    LocalAuthorityBinding,
    LocalWorkerIncarnation,
)
from kdive.worker_lifecycle.contracts import TerminationOutcome

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
        self.root = Path("/fixed-test-worker-state")
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
        self.load_failure: Exception | None = None
        self.persist_failure: Exception | None = None
        # A provisioned host always has the slot directory, so the default residue is never
        # EMPTY: the installer creates all eight. `state_document` models the raw state.json
        # independently of `state`, which is how cases 1 and 2 are constructed.
        self.directory = True
        self.state_document: str | None = "valid" if state is not None else None
        self.discards = 0

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
        self.state_document = "valid"
        self.events.append("persist:prepared")
        return self.state

    def load(self) -> SlotState | None:
        self.load_calls += 1
        if self.load_failure is not None:
            raise self.load_failure
        if self.state_document == "unreadable":
            raise StateConflict(f"slot {self.slot} state is malformed")
        return self.state

    def inspect(self) -> state_module.SlotInspection:
        self.load_calls += 1
        if not self.directory:
            return state_module.SlotInspection(state_module.SlotResidue.EMPTY, None)
        if self.state_document is None:
            return state_module.SlotInspection(state_module.SlotResidue.STATE_ABSENT, None)
        if self.state_document == "unreadable":
            return state_module.SlotInspection(state_module.SlotResidue.STATE_UNREADABLE, None)
        return state_module.SlotInspection(state_module.SlotResidue.STATE_VALID, self.state)

    def discard_unrecoverable(self) -> bool:
        self.discards += 1
        if not self.directory:
            return False
        removed = any(
            (self.state_document is not None, self.environment, self.credential, self.release)
        )
        self.state = None
        self.state_document = None
        self.environment = False
        self.credential = False
        self.release = False
        if removed:
            self.events.append(f"store:discard-unrecoverable:{self.slot}")
        return removed

    def persist(self, state: SlotState) -> None:
        if self.persist_failure is not None:
            raise self.persist_failure
        assert self.state is not None
        assert state.generation == self.state.generation
        self.state = state
        self.state_document = "valid"
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
        self.state_document = None
        self.events.append("state:discard-prepared")

    def cleanup_terminated(self, state: SlotState) -> None:
        assert state == self.state
        assert state.phase is SlotPhase.TERMINATED
        self.environment = False
        self.credential = False
        self.release = False
        self.state = None
        self.state_document = None
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
        self.journal_chunks: dict[str, tuple[str, ...]] = {}
        self.journal_failure: Exception | None = None
        self.public_property_calls: list[tuple[str, str]] = []
        self.stop_budgets: list[float] = []
        self.inactive_checks: list[tuple[str, float]] = []
        self.systemd_deadlines: list[tuple[str, Deadline]] = []
        # Units `systemctl stop` cannot clear. A unit left `failed` keeps its InvocationID until
        # `systemctl reset-failed`, which nothing in the coordinator runs, so `stop_retained` is a
        # no-op on it and the identity `require_inactive` rejects survives the cleanup.
        self.unstoppable: set[str] = set()

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
        # An untracked unit is the inactive, empty-identity case the real runtime answers with a
        # BootObservation, which is what every never-started fixed slot reports.
        observation = self.current.get(unit) or _boot_observation(_slot_from_unit(unit))
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
        if unit not in self.unstoppable:
            self.current.pop(unit, None)

    def reset_failed(self, unit: str, deadline: Deadline) -> None:
        self.systemd_deadlines.append(("reset-failed", deadline))
        assert deadline.remaining() >= 0
        self.resets.append(unit)
        self.events.append(f"systemd:reset-failed:{unit}")
        # `systemctl reset-failed` drops a failed unit's ActiveState and InvocationID, leaving
        # the inactive, empty-identity unit `observe` reports as a BootObservation.
        self.current.pop(unit, None)

    def unmanaged_workers(self) -> tuple[UnmanagedWorker, ...]:
        return self.unmanaged

    def public_properties(self, unit: str, invocation_id: str, deadline: Deadline) -> str:
        assert deadline.remaining() >= 0
        self.public_property_calls.append((unit, invocation_id))
        return "ActiveState=active\n"

    def journal(self, invocation_id: str, byte_limit: int, deadline: Deadline) -> tuple[str, ...]:
        self.journal_calls.append((invocation_id, byte_limit, deadline.remaining()))
        if self.journal_failure is not None:
            raise self.journal_failure
        return self.journal_chunks.get(invocation_id, ("untrusted journal text",))


class FakeAuthority:
    """Idempotent authority double with configurable dependency and evidence failures."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.registered: set[str] = set()
        self.register_label = "database:register"
        self.terminate_label = "database:terminate"
        self.fail_register = False
        self.fail_register_with_fence_conflict = False
        self.fail_terminate_with_fence_conflict = False
        self.reject_termination = False
        self.terminations: list[tuple[str, TerminationOutcome]] = []
        # The incarnation carries only unit and generation, so `terminations` alone cannot tell
        # evidence published for the retained invocation from evidence published for a successor's.
        # PostgreSQL rejects the wrong binding; this records what was actually offered to it.
        self.terminated_bindings: list[tuple[str, str | None, str | None]] = []
        # #2533: the rows a slot still holds, keyed by unit, and what was released from them.
        self.rows: dict[str, list[LocalWorkerIncarnation]] = {}
        # Snapshots taken when a row is handed out, so the release assertion compares values
        # rather than being satisfied by the record being the very object it was read from.
        self.row_snapshots: dict[str, dict[str, str]] = {}
        self.probed_units: list[str] = []
        self.released: list[tuple[str, TerminationOutcome]] = []
        self.recoverable_label = "database:recoverable"
        self.release_label = "database:release"
        self.fail_recoverable: Exception | None = None
        # `reject_termination` models the EVIDENCED path being refused because the retained
        # binding drifted from the row. `reject_release` is the separate case of the row's own
        # stored binding being refused -- which, by construction, the database cannot do unless
        # the row changed underneath us. Sharing one flag would have hidden the fallback.
        self.reject_release = False

    async def register(self, state: SlotState, credential_hash: bytes) -> None:
        assert credential_hash == bytes.fromhex(state.credential_hash)
        self.events.append(self.register_label)
        if self.fail_register_with_fence_conflict:
            raise IncarnationConflict(
                "worker incarnation registration conflicts with durable state"
            )
        if self.fail_register:
            raise RuntimeError("database unavailable")
        self.registered.add(state.incarnation)

    async def terminate(self, state: SlotState, outcome: TerminationOutcome) -> None:
        self.events.append(self.terminate_label)
        if self.fail_terminate_with_fence_conflict:
            raise IncarnationConflict("worker incarnation termination conflicts with durable state")
        if self.reject_termination:
            raise EvidenceRejected("database rejected exact evidence")
        assert state.incarnation in self.registered
        self.terminations.append((state.incarnation, outcome))
        self.terminated_bindings.append((state.incarnation, state.boot_id, state.invocation_id))
        # Both paths are the same real `terminate_worker_incarnation` call, so a row terminated
        # here stops being active exactly as one released through `release` does. Leaving it in
        # `rows` let a slot look swept while a row was still held.
        self.rows[state.unit] = [
            row for row in self.rows.get(state.unit, ()) if row.incarnation != state.incarnation
        ]

    async def recoverable(self, unit: str) -> tuple[LocalWorkerIncarnation, ...]:
        self.events.append(self.recoverable_label)
        self.probed_units.append(unit)
        if self.fail_recoverable is not None:
            raise self.fail_recoverable
        held = tuple(self.rows.get(unit, ()))
        for row in held:
            self.row_snapshots[row.incarnation] = {
                key: str(value) for key, value in row.authority_binding.items()
            }
        return held

    async def release(self, record: LocalWorkerIncarnation, outcome: TerminationOutcome) -> None:
        self.events.append(self.release_label)
        if self.reject_release:
            raise EvidenceRejected("database rejected the registered binding")
        # The design's whole claim is that the row's OWN stored binding satisfies the fence's
        # exact match. A fake that skipped this would pass while the real adapter failed.
        unit = record.authority_binding["unit"]
        held = self.rows.get(unit, ())
        assert record.incarnation in {row.incarnation for row in held}, (
            "released a row this slot does not hold"
        )
        offered = {key: str(value) for key, value in record.authority_binding.items()}
        assert offered == self.row_snapshots[record.incarnation], (
            "released a binding that is not the row's own stored binding"
        )
        self.rows[unit] = [row for row in held if row.incarnation != record.incarnation]
        self.released.append((record.incarnation, outcome))


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
        authority_instance="authority-a",
        authority_request_socket="/run/kdive/provider-authority/request/authority.sock",
        authority_server_ca_ref="external-boot-authority/server-ca",
        authority_client_certificate_ref="external-boot-authority/client-certificate",
        authority_client_key_ref="external-boot-authority/client-key",  # pragma: allowlist secret
        authority_store_identity="authority-recovery-store",
        authority_recovery_reserve_bytes=4096,
        authority_recovery_max_bytes=8192,
        external_boot_capacity_bytes=4096,
    )


def _request(worker_count: int = 1) -> LifecycleRequest:
    return LifecycleRequest(operation="start", worker_count=worker_count, settings=_settings())


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("authority_client_key_ref", "worker authority route must be complete or absent"),
        (
            "authority_store_identity",
            "worker authority reservation geometry must be complete or absent",
        ),
        (
            "authority_recovery_reserve_bytes",
            "worker authority reservation geometry must be complete or absent",
        ),
        (
            "authority_recovery_max_bytes",
            "worker authority reservation geometry must be complete or absent",
        ),
        (
            "external_boot_capacity_bytes",
            "worker authority reservation geometry must be complete or absent",
        ),
    ),
)
def test_worker_settings_reject_incomplete_authority_geometry(field: str, message: str) -> None:
    values = _settings().model_dump()
    values[field] = None
    with pytest.raises(ValueError, match=message):
        WorkerSettings.model_validate(values)


def test_worker_settings_allow_absent_geometry_and_reject_mismatched_geometry() -> None:
    values = _settings().model_dump()
    for field in (
        "authority_store_identity",
        "authority_recovery_reserve_bytes",
        "authority_recovery_max_bytes",
        "external_boot_capacity_bytes",
    ):
        values[field] = None
    assert WorkerSettings.model_validate(values).authority_store_identity is None

    values = _settings().model_dump()
    values["external_boot_capacity_bytes"] = 4097
    with pytest.raises(
        ValueError, match="worker authority reservation geometry must match worker capacity"
    ):
        WorkerSettings.model_validate(values)


def _mutations(events: list[str]) -> list[str]:
    """Drop the read-only authority probe `recover` issues for every slot (#2533).

    Recovery must ask the database whether each slot still holds a fence, because a slot whose
    `state.json` is gone looks empty on disk while its row is still active -- that is #2533's
    case 1. The read mutates nothing, so assertions about what recovery *did* filter it out.
    """
    return [event for event in events if event != "database:recoverable"]


def _row(
    slot: int,
    *,
    generation: str | None = None,
    boot_id: str = _BOOT_ID,
    invocation_id: str | None = None,
    host: str | None = None,
    unit: str | None = None,
) -> LocalWorkerIncarnation:
    """One active `worker_incarnations` row as the recovery accessor returns it."""
    resolved_unit = unit or f"kdive-live-worker@{slot}.service"
    resolved_generation = generation or f"{slot:x}" * 32
    binding = LocalAuthorityBinding(
        unit=resolved_unit,
        generation=resolved_generation,
        boot_id=boot_id,
        invocation_id=invocation_id or f"{slot:x}" * 32,
        host=host or socket.gethostname(),
    )
    return LocalWorkerIncarnation(
        f"local-systemd:kdive-live-worker@{slot}.service:{resolved_generation}",
        "local",
        binding,
        4,
    )


def _deadline(clock: FakeClock, seconds: float = 1_000.0) -> MonotonicDeadline:
    return MonotonicDeadline.after(seconds, monotonic=clock)


def _coordinator(
    stores: list[FakeStore],
    runtime: FakeRuntime,
    authority: FakeAuthority,
    clock: FakeClock,
    *,
    redaction_sources: dict[int, tuple[str, ...]] | None = None,
) -> SystemdWorkerLifecycle:
    sources = redaction_sources or {}
    return SystemdWorkerLifecycle(
        stores=tuple(stores),
        runtime=runtime,
        authority=authority,
        wait=clock.advance,
        load_redaction_values=lambda _root, slot: sources.get(slot, ()),
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


def _assert_retained_binding_retired(authority: FakeAuthority, state: SlotState) -> None:
    """Assert the retained binding was retired as ``killed``, and never the successor's.

    A coordinator that released the observed invocation instead would publish the same
    incarnation and the same outcome, so only the binding separates the two.
    """
    assert authority.terminations == [(state.incarnation, "killed")]
    assert authority.terminated_bindings == [
        (state.incarnation, state.boot_id, state.invocation_id)
    ]


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
    assert response.message == "retained lifecycle facts conflict with the observed unit"
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
    assert response.message == "systemd observation conflicts with the retained lifecycle contract"
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
    last_stop = max(
        index
        for index, (operation, _) in enumerate(runtime.systemd_deadlines)
        if operation == "stop-retained"
    )
    assert (
        len(
            {
                id(deadline)
                for _, deadline in runtime.systemd_deadlines[first_signal : last_stop + 1]
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


def test_same_boot_successor_invocation_retires_the_retained_incarnation() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "populated", invocation_id="f" * 32)

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert response.ok
    _assert_retained_binding_retired(authority, started)
    assert runtime.signaled == []
    assert runtime.stopped == [started.unit]
    assert stores[0].state is None
    assert not stores[0].environment and not stores[0].credential and not stores[0].release


def test_start_reconciles_a_restarted_unit_and_replaces_the_slot() -> None:
    # A successor `systemctl stop` can clear, so activation reaches an inactive unit with an empty
    # identity. The failed-successor case the strict gate actually produces is the next test.
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "populated", invocation_id="f" * 32)

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    assert response.ok
    _assert_retained_binding_retired(authority, started)
    assert stores[0].state is not None and stores[0].state.phase is SlotPhase.STARTED
    assert stores[0].state.generation != started.generation


def test_start_retires_a_restarted_slot_whose_successor_unit_stays_failed() -> None:
    # The residual ADR-0657 discloses and #2488 owns. Under the strict gate binding the successor
    # invocation exits non-zero, so its unit keeps ActiveState=failed and its InvocationID;
    # `systemctl stop` is a no-op on such a unit and nothing runs `systemctl reset-failed`. The
    # retained binding is still retired through the contract, which is what this change delivers,
    # but `require_inactive` then refuses to activate a replacement.
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(
        1, "empty", invocation_id="f" * 32, result="exit-code", status=1
    )
    runtime.unstoppable.add(started.unit)

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(), _deadline(clock))
    )

    _assert_retained_binding_retired(authority, started)
    assert stores[0].state is None
    assert not stores[0].environment and not stores[0].credential and not stores[0].release
    assert (response.code, response.retry_action) == ("conflict", "operator_recovery")


def test_out_of_band_replacement_is_logged_before_the_slot_files_are_deleted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "populated", invocation_id="f" * 32)

    with caplog.at_level("WARNING"):
        response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert response.ok
    assert "retained worker invocation was replaced out of band" in caplog.text
    assert started.unit in caplog.text
    assert "f" * 32 in caplog.text
    # The outcome alone cannot carry this: it is the same `killed` any unobservable termination
    # gets, and the slot files that would hold the timeline are gone by the time the call returns.
    assert stores[0].state is None


def test_successor_invocation_exit_facts_are_not_attributed_to_the_retained_one() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(
        1, "empty", invocation_id="f" * 32, result="exit-code", status=2
    )

    response = _run(_coordinator(stores, runtime, authority, clock).status(_deadline(clock)))

    assert response.ok
    _assert_retained_binding_retired(authority, started)


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


def test_partial_start_logs_bounded_rollback_failure(caplog: pytest.LogCaptureFixture) -> None:
    stores, runtime, authority, clock, _ = _fleet()
    runtime.start_failures["kdive-live-worker@2.service"] = SystemdUnavailable(
        "system manager unavailable"
    )
    authority.reject_termination = True

    response = _run(
        _coordinator(stores, runtime, authority, clock).start(_request(2), _deadline(clock))
    )

    assert response.code == "dependency_unavailable"
    assert "cause=EvidenceRejected cleaned_slots=[]" in caplog.text
    assert "database rejected exact evidence" not in caplog.text


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
        "state:cleanup",
    ]
    assert runtime.start_counts == {}
    assert prepared.incarnation in authority.registered
    assert authority.terminations == [(prepared.incarnation, "succeeded")]


def test_state_conflict_reports_its_own_distinct_message() -> None:
    prepared = _state(1, SlotPhase.PREPARED)
    stores, runtime, authority, clock, _ = _fleet(states={1: prepared})
    runtime.current[prepared.unit] = _observation(1, "populated")
    stores[0].persist_failure = StateConflict("slot state is malformed")

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert (response.code, response.retry_action) == ("conflict", "operator_recovery")
    assert response.message == "retained slot state conflicts with lifecycle rules"
    assert authority.registered == set() and authority.terminations == []


def test_fence_refused_registration_reports_a_fence_conflict_not_a_database_outage() -> None:
    # #2481: a still-active fence refusing this registration was misreported as a database
    # outage, because IncarnationConflict fell through _register's generic exception wrapper.
    prepared = _state(1, SlotPhase.PREPARED)
    stores, runtime, authority, clock, _ = _fleet(states={1: prepared})
    runtime.current[prepared.unit] = _observation(1, "populated")
    authority.fail_register_with_fence_conflict = True

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert (response.code, response.retry_action) == ("conflict", "operator_recovery")
    assert response.message == "worker incarnation conflicts with an active fence"
    assert authority.registered == set() and authority.terminations == []


def test_fence_refused_termination_reports_a_fence_conflict_not_a_database_outage() -> None:
    # No current backend's `terminate()` raises IncarnationConflict — only `register`'s
    # unique-violation path does today (`authority_store.py`). This pins `_terminate`'s
    # symmetric handling for the recovery-termination path issue #2488 adds, not a
    # currently reachable production misattribution.
    prepared = _state(1, SlotPhase.PREPARED)
    stores, runtime, authority, clock, _ = _fleet(states={1: prepared})
    runtime.current[prepared.unit] = _observation(1, "populated")
    authority.fail_terminate_with_fence_conflict = True

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert (response.code, response.retry_action) == ("conflict", "operator_recovery")
    assert response.message == "worker incarnation conflicts with an active fence"
    assert prepared.incarnation in authority.registered
    assert authority.terminations == []


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


def test_foreign_unit_observation_is_refused_without_evidence() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(2, "populated")

    response = _run(_coordinator(stores, runtime, authority, clock).status(_deadline(clock)))

    assert (response.code, response.retry_action) == ("conflict", "operator_recovery")
    assert authority.terminations == []
    assert stores[0].state == started


def test_unknown_membership_on_the_retained_invocation_is_not_terminal_evidence() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "unknown")

    response = _run(_coordinator(stores, runtime, authority, clock).status(_deadline(clock)))

    assert (response.code, response.retry_action) == ("dependency_unavailable", "restore_systemd")
    assert authority.terminations == []
    assert stores[0].state == started


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


@pytest.mark.parametrize("live_cgroup", (False, True))
def test_reboot_maps_exact_retained_binding_to_killed(live_cgroup: bool) -> None:
    # The prior boot's cgroup cannot survive a reboot, so the boot-ID rule decides both an absent
    # unit and one whose properties still look live, before membership is ever read.
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = (
        _observation(1, "populated", boot_id=_NEXT_BOOT_ID)
        if live_cgroup
        else _boot_observation(1, boot_id=_NEXT_BOOT_ID)
    )

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


def test_stop_commits_evidence_before_unit_and_state_cleanup() -> None:
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
        "state:cleanup",
    ]
    stop_path_deadlines = [
        deadline
        for operation, deadline in runtime.systemd_deadlines
        if operation in {"observe", "signal-terminate", "stop-retained"}
    ]
    assert len({id(deadline) for deadline in stop_path_deadlines}) == 1


def test_stop_cleanup_does_not_reset_a_stopped_template_instance() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: started})

    response = _run(_coordinator(stores, runtime, authority, clock).stop(_deadline(clock)))

    assert response.ok
    assert stores[0].state is None
    assert runtime.stopped == [started.unit]
    assert runtime.resets == []
    assert events[-2:] == ["systemd:stop:kdive-live-worker@1.service", "state:cleanup"]


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
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("credential-1",)},
    )

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

    assert diagnostics.ok
    assert diagnostics.code == "ok"
    assert diagnostics.diagnostics is not None
    assert "ActiveState=active" in diagnostics.diagnostics
    assert "ControlGroup=" not in diagnostics.diagnostics
    assert "InvocationID=" not in diagnostics.diagnostics
    assert "untrusted journal text" in diagnostics.diagnostics
    assert runtime.journal_calls[0][0] == started.invocation_id
    assert 0 < runtime.journal_calls[0][1] <= 320 * 1024
    assert runtime.journal_calls[0][2] == 30.0
    assert stores[0].state == terminated
    assert stores[0].environment and stores[0].credential and stores[0].release

    stopped = _run(coordinator.stop(_deadline(clock)))

    assert stopped.ok
    assert stores[0].state is None
    assert runtime.stopped == [started.unit]
    assert runtime.resets == []


def test_diagnostics_uses_one_30_second_acquisition_ceiling_and_never_mutates() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in (1, 2)}
    stores, runtime, authority, clock, events = _fleet(states=states)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("credential-1",), 2: ("credential-2",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok
    assert response.code == "ok"
    assert [call[0] for call in runtime.journal_calls] == [
        states[1].invocation_id,
        states[2].invocation_id,
    ]
    assert all(budget <= 30.0 for _, _, budget in runtime.journal_calls)
    assert events == []
    assert stores[0].state == states[1] and stores[1].state == states[2]


@pytest.mark.parametrize("boundary", ("first", "middle", "last"))
@pytest.mark.parametrize(
    ("template", "secret", "registered"),
    (
        ("credential={secret}", "RETAINED-CREDENTIAL-UNIQUE", True),
        (
            "database={secret}",
            "postgresql://DB-USER:DB-PASSWORD@localhost/kdive",  # pragma: allowlist secret
            True,
        ),
        ("s3={secret}", "OBJECT-STORE-SECRET-UNIQUE", True),
        ("url=postgresql://{secret}@localhost/kdive", "URL-USER:URL-PASSWORD", False),
        ("PASSWORD={secret}", "STRUCTURAL-PASSWORD-UNIQUE", False),
    ),
)
def test_diagnostics_redacts_secrets_split_across_journal_chunks(
    boundary: str, template: str, secret: str, registered: bool
) -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: state})
    offsets = {"first": 1, "middle": len(secret) // 2, "last": len(secret) - 1}
    secret_offset = offsets[boundary]
    text = template.format(secret=secret)
    split = text.index(secret) + secret_offset
    runtime.journal_chunks[state.invocation_id or ""] = (text[:split], text[split:])
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: (secret,) if registered else ("retained-credential",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    left, right = secret[:secret_offset], secret[secret_offset:]
    assert secret not in response.diagnostics
    assert left + right not in response.diagnostics
    lines = response.diagnostics.splitlines()
    assert all(
        secret not in first + second for first, second in zip(lines, lines[1:], strict=False)
    )
    if template.startswith("url="):
        assert "url=postgresql://" in response.diagnostics
        assert "@localhost/kdive" in response.diagnostics
    assert events == []
    assert stores[0].state == state


def test_diagnostics_applies_structural_and_control_redaction_before_bounding() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: state})
    hostile = (
        "postgresql://struct-user:struct-pass@localhost/kdive "  # pragma: allowlist secret
        "DATABASE_URL=another-secret PASSWORD=hunter2\n::error::oops\x00\x01"
    )
    runtime.journal_chunks[state.invocation_id or ""] = (hostile, "x" * (320 * 1024))
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("retained-credential",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    output = response.diagnostics
    for secret in ("struct-user", "struct-pass", "another-secret", "hunter2"):
        assert secret not in output
    assert "::error::" not in output
    assert "\x00" not in output and "\x01" not in output
    assert "\\x00" in output and "\\x01" in output
    assert output.count("[diagnostics truncated]") == 1
    assert len(output.encode("utf-8")) <= 256 * 1024
    assert events == []


def test_diagnostics_enforces_aggregate_acquisition_and_emission_limits() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in range(1, 9)}
    stores, runtime, authority, clock, events = _fleet(states=states)
    for state in states.values():
        runtime.journal_chunks[state.invocation_id or ""] = ("x" * (400 * 1024),)
    sources: dict[int, tuple[str, ...]] = {slot: (f"credential-{slot}",) for slot in states}
    coordinator = _coordinator(stores, runtime, authority, clock, redaction_sources=sources)

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert len(runtime.journal_calls) == 4
    assert sum(call[1] + 4096 for call in runtime.journal_calls) <= 1_310_720
    assert all(call[1] <= 320 * 1024 for call in runtime.journal_calls)
    assert len(response.diagnostics.encode("utf-8")) <= 1_048_576
    assert events == []
    assert all(store.state == states[store.slot] for store in stores)


def test_diagnostics_reserves_aggregate_acquisition_for_failed_journals() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in range(1, 9)}
    stores, runtime, authority, clock, _ = _fleet(states=states)
    runtime.journal_failure = SystemdUnavailable("journal unavailable")
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={slot: (f"retained-{slot}",) for slot in states},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert not response.ok and response.diagnostics is not None
    assert len(runtime.journal_calls) == 4
    assert sum(byte_limit + 4096 for _, byte_limit, _ in runtime.journal_calls) <= 1_310_720
    withheld = "".join(
        f"[diagnostics withheld for slot {slot}: acquisition_failed]\n" for slot in range(1, 5)
    )
    assert response.diagnostics == f"{withheld}[aggregate diagnostics truncated]\n"


def test_failed_journal_aggregate_marker_respects_known_forbidden_values() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in range(1, 9)}
    stores, runtime, authority, clock, _ = _fleet(states=states)
    runtime.journal_failure = SystemdUnavailable("journal unavailable")
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={slot: ("aggregate",) for slot in states},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    # Only the aggregate marker carries the forbidden value, so only it is suppressed; each
    # withheld slot still names its cause.
    assert not response.ok
    assert response.diagnostics == "".join(
        f"[diagnostics withheld for slot {slot}: acquisition_failed]\n" for slot in range(1, 5)
    )
    assert "[aggregate diagnostics truncated]" not in (response.diagnostics or "")
    assert len(runtime.journal_calls) == 4


def test_diagnostics_withholds_unsafe_source_without_reading_its_journal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: state})

    def unsafe_source(_root: Path, _slot: int) -> tuple[str, ...]:
        raise PermissionError("sensitive source path and metadata")

    coordinator = SystemdWorkerLifecycle(
        stores=tuple(stores),
        runtime=runtime,
        authority=authority,
        wait=clock.advance,
        load_redaction_values=unsafe_source,
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert not response.ok
    assert response.code == "diagnostics_withheld"
    # The shipped loader funnels every failure into PermissionError, so this is the one producer
    # of internal_error: it is what keeps the generic arm, and that vocabulary member, reachable.
    assert response.diagnostics == "[diagnostics withheld for slot 1: internal_error]\n"
    assert response.slots[0].message == "withheld: internal_error"
    assert response.slots[0].phase is SlotPhase.STARTED
    assert "sensitive" not in response.model_dump_json()
    assert runtime.public_property_calls == []
    assert runtime.journal_calls == []
    assert events == []
    assert stores[0].state == state
    assert "slot=1 cause=PermissionError" in caplog.text
    assert "sensitive source path" not in caplog.text


def test_diagnostics_withholds_unsafe_state_without_exposing_error_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: state})
    stores[0].load_failure = ValueError("state path and credential detail")
    coordinator = _coordinator(stores, runtime, authority, clock)

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert not response.ok
    assert response.code == "diagnostics_withheld"
    assert response.diagnostics == "[diagnostics withheld for slot 1: state_unreadable]\n"
    assert response.slots[0].message == "withheld: state_unreadable"
    # No state was loadable, so the site has no phase to report.
    assert response.slots[0].phase is None
    assert "credential detail" not in response.model_dump_json()
    assert runtime.journal_calls == []
    assert events == []
    assert "slot=1 cause=ValueError" in caplog.text
    assert "credential detail" not in caplog.text


def test_diagnostics_withholds_oversized_redaction_value() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: state})
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("s" * 4097,)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert not response.ok
    assert response.diagnostics == "[diagnostics withheld for slot 1: slot_unusable]\n"
    assert response.slots[0].code == "diagnostics_withheld"
    assert response.slots[0].message == "withheld: slot_unusable"
    assert runtime.journal_calls == []
    assert events == []


def test_diagnostics_names_acquisition_failed_reason() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: state})
    runtime.journal_failure = SystemdUnavailable("journal unavailable")
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("retained-credential",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert not response.ok and response.code == "diagnostics_withheld"
    assert response.diagnostics == "[diagnostics withheld for slot 1: acquisition_failed]\n"
    assert response.slots[0].code == "diagnostics_withheld"
    assert response.slots[0].message == "withheld: acquisition_failed"
    assert response.slots[0].phase is SlotPhase.STARTED
    assert events == []


def test_diagnostics_names_peer_redaction_refused_reason() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in (1, 2)}
    stores, runtime, authority, clock, events = _fleet(states=states)
    runtime.journal_chunks[states[2].invocation_id or ""] = ("saw ALPHACREDENTIAL",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("ALPHACREDENTIAL",), 2: ("BETACREDENTIAL",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    # Slot 2 renders clean against its own forbidden set and is refused only against the set
    # slot 1 contributed, which is the one condition this reason separates from the rest.
    assert not response.ok and response.code == "diagnostics_withheld"
    assert response.slots[0].code == "ok"
    assert response.slots[1].code == "diagnostics_withheld"
    assert response.slots[1].message == "withheld: peer_redaction_refused"
    assert response.slots[1].phase is SlotPhase.STARTED
    assert response.diagnostics is not None
    assert response.diagnostics.endswith(
        "[diagnostics withheld for slot 2: peer_redaction_refused]\n"
    )
    assert "ALPHACREDENTIAL" not in response.model_dump_json()
    assert events == []


def test_diagnostics_names_redaction_refused_when_no_sentinel_survives() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: state})
    # Escaping the NUL reintroduces the literal "x00" the redactor just masked, so no sentinel
    # choice renders the text safely and `_sanitize_diagnostics` refuses. That refusal is a
    # StateConflict raised inside `_diagnose_slot`'s try, where `acquisition_failures` would
    # otherwise relabel it as an acquisition failure the operator is told to retry.
    runtime.journal_chunks[state.invocation_id or ""] = ("saw \x00 here",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("x00",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert not response.ok and response.code == "diagnostics_withheld"
    assert response.diagnostics == "[diagnostics withheld for slot 1: redaction_refused]\n"
    assert response.slots[0].message == "withheld: redaction_refused"
    assert events == []


def test_diagnostics_reason_carries_no_withheld_material() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: state})
    runtime.journal_chunks[state.invocation_id or ""] = ("LEAK-SENTINEL" + "x" * (320 * 1024),)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("truncated",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert not response.ok and response.code == "diagnostics_withheld"
    assert response.diagnostics == "[diagnostics withheld for slot 1: redaction_refused]\n"
    assert response.slots[0].message == "withheld: redaction_refused"
    assert "LEAK-SENTINEL" not in response.model_dump_json()
    assert events == []


def test_diagnostics_withheld_marker_respects_known_forbidden_values() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: state})
    runtime.journal_failure = SystemdUnavailable("journal unavailable")
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("withheld",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    # The marker itself carries a forbidden value, so it is suppressed whole; the reason still
    # reaches the operator on the slot result, which is composed only of closed-enum literals.
    assert not response.ok and response.diagnostics == ""
    assert response.slots[0].code == "diagnostics_withheld"
    assert response.slots[0].message == "withheld: acquisition_failed"
    assert events == []


def test_diagnostics_removes_overlapping_secret_literals_longest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    runtime.journal_chunks[state.invocation_id or ""] = ("saw OVERLAP-SUFFIX",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("OVERLAP", "OVERLAP-SUFFIX")},
    )
    monkeypatch.setattr(
        SecretRegistry,
        "snapshot",
        lambda _registry: ("OVERLAP", "OVERLAP-SUFFIX"),
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert "OVERLAP" not in response.diagnostics
    assert "SUFFIX" not in response.diagnostics


def test_diagnostics_does_not_move_acquisition_guard_bytes_into_emission() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    secret = "S" * 4096
    full_literals = (secret + "\n") * 16
    journal_budget = 320 * 1024 - 4096
    padding = "x" * (journal_budget - len(full_literals) - 2000)
    runtime.journal_chunks[state.invocation_id or ""] = (full_literals + padding + secret[:2000],)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: (secret,)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert "S" * 100 not in response.diagnostics
    assert len(response.diagnostics.encode()) <= 256 * 1024


@pytest.mark.parametrize("secret", ("[REDACTED]", "ACT"))
def test_diagnostics_redaction_marker_cannot_reproduce_a_secret(secret: str) -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    runtime.journal_chunks[state.invocation_id or ""] = (f"literal={secret}",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: (secret,)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert secret not in response.diagnostics


def test_diagnostics_sanitizes_framework_headers_with_registered_values() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    runtime.journal_chunks[state.invocation_id or ""] = ("payload",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("slot",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert "slot" not in response.diagnostics
    assert "payload" in response.diagnostics


@pytest.mark.parametrize(
    ("secret", "expected"),
    (
        # The withhold marker itself holds "diagnostics", so it is suppressed whole; it holds no
        # "truncated", so that parameter emits it. Each side of the suppression rule, stated.
        ("diagnostics", ""),
        ("truncated", "[diagnostics withheld for slot 1: redaction_refused]\n"),
    ),
)
def test_diagnostics_emits_no_fallback_when_truncation_text_collides(
    secret: str, expected: str
) -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    runtime.journal_chunks[state.invocation_id or ""] = ("x" * (320 * 1024),)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: (secret,)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert not response.ok and response.diagnostics is not None
    assert response.diagnostics == expected
    assert secret not in response.diagnostics
    assert "x" not in response.diagnostics
    assert response.slots[0].message == "withheld: redaction_refused"


def test_diagnostics_mask_cannot_reproduce_an_unknown_structural_secret() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    runtime.journal_chunks[state.invocation_id or ""] = ("PASSWORD=~",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("retained",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert "~" not in response.diagnostics


def test_diagnostics_selects_beyond_the_previous_finite_mask_set() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    secret = "█~^#%?"
    runtime.journal_chunks[state.invocation_id or ""] = (f"literal={secret}",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: (secret,)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert secret not in response.diagnostics
    assert "!" in response.diagnostics


def test_diagnostics_retries_a_sentinel_that_collides_after_control_escaping() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    occupied = "█!\"#$%&'()*+,-./0123456789"
    runtime.journal_chunks[state.invocation_id or ""] = (f"literal={occupied}",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: (occupied, "x3a")},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert occupied not in response.diagnostics
    assert "x3a" not in response.diagnostics
    assert ";" in response.diagnostics


def test_diagnostics_bounds_render_attempts_for_near_limit_structural_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    padding = "x" * (256 * 1024 - 100)
    runtime.journal_chunks[state.invocation_id or ""] = (f"{padding}\nPASSWORD=slot",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("retained",)},
    )
    real_render = diagnostics_module._render_sanitized_diagnostics
    attempts = 0

    def bounded_render(
        text: str,
        registered: tuple[str, ...],
        sentinel: str,
        *,
        acquisition_truncated: bool,
    ) -> str:
        nonlocal attempts
        attempts += 1
        if attempts > 2:
            raise AssertionError("diagnostic sanitizer exceeded two full render attempts")
        return real_render(
            text,
            registered,
            sentinel,
            acquisition_truncated=acquisition_truncated,
        )

    monkeypatch.setattr(diagnostics_module, "_render_sanitized_diagnostics", bounded_render)

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert attempts == 1
    assert "slot" not in response.diagnostics
    assert len(response.diagnostics.encode()) <= 256 * 1024


def test_diagnostics_escapes_unicode_format_and_separator_controls() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    controls = "\u200b\u202e\u2066\u2028\u2029"
    runtime.journal_chunks[state.invocation_id or ""] = (f"before{controls}after",)
    coordinator = _coordinator(
        stores, runtime, authority, clock, redaction_sources={1: ("retained",)}
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert all(character not in response.diagnostics for character in controls)
    assert "\\u200b\\u202e\\u2066\\u2028\\u2029" in response.diagnostics


@pytest.mark.parametrize(
    ("journal", "leaked"),
    (
        (
            "PASSWORD='quoted-secret with suffix' visible",  # pragma: allowlist secret
            ("quoted-secret", "suffix", "visible"),
        ),
        (
            'API_TOKEN="unterminated-secret suffix',  # pragma: allowlist secret
            ("unterminated-secret", "suffix"),
        ),
    ),
)
def test_diagnostics_masks_the_rest_of_a_structural_secret_assignment(
    journal: str, leaked: tuple[str, ...]
) -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    runtime.journal_chunks[state.invocation_id or ""] = (journal,)
    coordinator = _coordinator(
        stores, runtime, authority, clock, redaction_sources={1: ("retained",)}
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert all(value not in response.diagnostics for value in leaked)


def test_diagnostics_masks_unterminated_url_userinfo_beyond_acquisition_guard() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    report_prefix = "=== slot 1 ===\nActiveState=active\nJournal:\n"
    padding = "x" * (256 * 1024 - len(report_prefix.encode()) - 100)
    userinfo = "U" * (70 * 1024)
    runtime.journal_chunks[state.invocation_id or ""] = (
        f"{padding}postgresql://{userinfo}@localhost/kdive",
    )
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("retained",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert "U" * 50 not in response.diagnostics
    assert len(response.diagnostics.encode()) <= 256 * 1024


def test_diagnostics_masks_unterminated_schemeless_userinfo_beyond_guard() -> None:
    state = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: state})
    report_prefix = "=== slot 1 ===\nActiveState=active\nJournal:\n"
    padding = "x" * (256 * 1024 - len(report_prefix.encode()) - 101)
    password = "P" * (70 * 1024)
    runtime.journal_chunks[state.invocation_id or ""] = (f"{padding} user:{password}@host",)
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={1: ("retained",)},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert "P" * 50 not in response.diagnostics
    assert len(response.diagnostics.encode()) <= 256 * 1024


def test_diagnostics_reserves_an_aggregate_truncation_marker() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in range(1, 6)}
    stores, runtime, authority, clock, _ = _fleet(states=states)
    for slot, state in states.items():
        prefix = f"=== slot {slot} ===\nActiveState=active\nJournal:\n"
        runtime.journal_chunks[state.invocation_id or ""] = (
            "x" * (256 * 1024 - len(prefix.encode())),
        )
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={slot: (f"retained-{slot}",) for slot in states},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert response.ok and response.diagnostics is not None
    assert response.diagnostics.endswith("[aggregate diagnostics truncated]\n")
    assert response.diagnostics.count("[aggregate diagnostics truncated]") == 1
    assert len(response.diagnostics.encode()) <= 1_048_576


def test_diagnostics_emits_no_fallback_when_aggregate_marker_collides() -> None:
    states = {slot: _state(slot, SlotPhase.STARTED) for slot in range(1, 6)}
    stores, runtime, authority, clock, _ = _fleet(states=states)
    for slot, state in states.items():
        prefix = f"=== slot {slot} ===\nActiveState=active\nJournal:\n"
        runtime.journal_chunks[state.invocation_id or ""] = (
            "x" * (256 * 1024 - len(prefix.encode())),
        )
    coordinator = _coordinator(
        stores,
        runtime,
        authority,
        clock,
        redaction_sources={slot: ("aggregate",) for slot in states},
    )

    response = _run(coordinator.diagnostics(_deadline(clock)))

    assert not response.ok and response.diagnostics is not None
    assert "aggregate" not in response.model_dump_json()
    # Slot 4's marker names redaction_refused and holds no "aggregate", so it is emitted where
    # the aggregate-truncation marker it replaces was suppressed.
    marker = b"[diagnostics withheld for slot 4: redaction_refused]\n"
    assert response.diagnostics.endswith(marker.decode())
    assert response.slots[3].message == "withheld: redaction_refused"
    # Exact, not a bound: LifecycleResponse already rejects anything over 1 MiB, so a <= assertion
    # here could not fail, and an under-emitting regression would pass it silently.
    assert len(response.diagnostics.encode()) == 3 * 256 * 1024 + len(marker)


class _DiagnosticPropertyRunner:
    def __init__(self, invocation_id: str) -> None:
        self.invocation_id = invocation_id

    def run(
        self,
        argv: Sequence[str],
        *,
        byte_limit: int,
        deadline: Deadline | None = None,
        allow_truncation: bool = False,
    ) -> str:
        assert argv[0] == "systemctl"
        assert byte_limit == 4096
        assert deadline is not None
        assert not allow_truncation
        return (
            "ActiveState=failed\n"
            "SubState=failed\n"
            "Result=exit-code\n"
            "ExecMainStatus=7\n"
            "ControlGroup=/system.slice/system-kdive\\x2dlive\\x2dworker.slice/"
            "kdive-live-worker@1.service\n"
            f"InvocationID={self.invocation_id}\n"
        )


def test_runtime_diagnostic_properties_emit_only_the_public_allowlist() -> None:
    invocation_id = "1" * 32
    runtime = SystemdRuntime(_DiagnosticPropertyRunner(invocation_id))

    output = runtime.public_properties(
        "kdive-live-worker@1.service", invocation_id, _deadline(FakeClock())
    )

    assert output == ("ActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=7\n")
    assert "ControlGroup" not in output and "InvocationID" not in output


def _diagnostic_source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "live-workers"
    slots = root / "slots"
    slot = slots / "1"
    slot.mkdir(parents=True)
    root.chmod(0o755)
    slots.chmod(0o711)
    slot.chmod(0o750)
    credential = slot / "worker-incarnation.credential"
    credential.write_text("retained-credential", encoding="utf-8")
    credential.chmod(0o400)
    environment = slot / "worker.env"
    environment.write_text(
        "AWS_ACCESS_KEY_ID=access-key\n"
        "AWS_SECRET_ACCESS_KEY=object-secret\n"
        "KDIVE_DATABASE_URL="
        "postgresql://worker:password@localhost/kdive\n"  # pragma: allowlist secret
        "KDIVE_API_TOKEN=future-token\n"
        "KDIVE_LOG_LEVEL=INFO\n",
        encoding="utf-8",
    )
    environment.chmod(0o600)
    return root


def test_diagnostic_source_loader_returns_only_secret_classified_values(tmp_path: Path) -> None:
    root = _diagnostic_source_tree(tmp_path)

    values = load_slot_redaction_values(root, 1, expected_uid=os.getuid(), expected_gid=os.getgid())

    assert set(values) == {
        "retained-credential",
        "access-key",
        "object-secret",
        "future-token",
        "postgresql://worker:password@localhost/kdive",  # pragma: allowlist secret
    }
    assert "INFO" not in values


def test_diagnostic_loader_reads_a_real_prepared_slot_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "live-workers"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    monkeypatch.setattr(state_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(state_module.os, "fchown", lambda _fd, _uid, _gid: None)
    monkeypatch.setattr(
        state_module.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_gid=os.getgid()),
    )
    store = SlotStore(root=root, slot=1)
    store.prepare(_settings())

    values = load_slot_redaction_values(
        root,
        1,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    assert _stat_mode(root / "slots") == 0o711
    assert {"access", "secret", "postgresql://worker@localhost/kdive"} <= set(values)
    environment = (root / "slots/1/worker.env").read_text(encoding="utf-8")
    assert "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE=" + "authority-a\n" in environment
    assert "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET=" in environment
    assert "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF=" in environment
    assert "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF=" in environment
    assert "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF=" in environment
    store_identity = "authority-recovery-store"  # pragma: allowlist secret - fixture identity
    assert f"KDIVE_EXTERNAL_BOOT_AUTHORITY_STORE_IDENTITY={store_identity}\n" in environment
    assert "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_RESERVE_BYTES=4096\n" in environment
    assert "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_MAX_BYTES=8192\n" in environment
    assert "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES=4096\n" in environment


def test_generated_worker_environment_satisfies_local_authority_reservation_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "live-workers"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    monkeypatch.setattr(state_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(state_module.os, "fchown", lambda _fd, _uid, _gid: None)
    monkeypatch.setattr(
        state_module.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_gid=os.getgid()),
    )
    SlotStore(root=root, slot=1).prepare(_settings())

    environment = {
        name: value
        for line in (root / "slots/1/worker.env").read_text(encoding="utf-8").splitlines()
        for name, value in (shlex.split(line)[0].split("=", 1),)
    }
    config_registry.load(environment)

    assert authority_reservation_geometry(
        ProviderBinding(ResourceKind.LOCAL_LIBVIRT, build_runtime(secret_registry=SecretRegistry()))
    ) == AuthorityReservationGeometry("authority-recovery-store", 4096, 8192)


def _stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_diagnostic_source_loader_reads_to_eof_after_a_short_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _diagnostic_source_tree(tmp_path)
    environment = root / "slots/1/worker.env"
    prefix_size = environment.read_bytes().index(b"KDIVE_API_TOKEN")
    real_read = os.read
    shortened = False

    def short_read(descriptor: int, count: int) -> bytes:
        nonlocal shortened
        path = os.readlink(f"/proc/self/fd/{descriptor}")
        if path == str(environment) and not shortened:
            shortened = True
            return real_read(descriptor, min(prefix_size, count))
        return real_read(descriptor, count)

    monkeypatch.setattr(runtime_module.os, "read", short_read)

    values = load_slot_redaction_values(root, 1, expected_uid=os.getuid(), expected_gid=os.getgid())

    assert "future-token" in values


def test_diagnostic_source_loader_rejects_post_read_metadata_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _diagnostic_source_tree(tmp_path)
    environment = root / "slots/1/worker.env"
    real_fstat = os.fstat
    environment_stats = 0

    def changing_fstat(descriptor: int) -> os.stat_result:
        nonlocal environment_stats
        metadata = real_fstat(descriptor)
        if os.readlink(f"/proc/self/fd/{descriptor}") != str(environment):
            return metadata
        environment_stats += 1
        if environment_stats == 1:
            return metadata
        fields = list(metadata)
        fields[6] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(runtime_module.os, "fstat", changing_fstat)

    with pytest.raises(PermissionError, match="source is unsafe"):
        load_slot_redaction_values(root, 1, expected_uid=os.getuid(), expected_gid=os.getgid())


@pytest.mark.parametrize("unsafe_kind", ("symlink", "nonregular", "mode", "owner"))
def test_diagnostic_source_loader_rejects_unsafe_metadata(tmp_path: Path, unsafe_kind: str) -> None:
    root = _diagnostic_source_tree(tmp_path)
    credential = root / "slots/1/worker-incarnation.credential"
    expected_uid = os.getuid()
    if unsafe_kind == "symlink":
        target = tmp_path / "credential-target"
        target.write_text("retained-credential", encoding="utf-8")
        target.chmod(0o400)
        credential.unlink()
        credential.symlink_to(target)
    elif unsafe_kind == "nonregular":
        credential.unlink()
        credential.mkdir(mode=0o400)
    elif unsafe_kind == "mode":
        credential.chmod(0o440)
    else:
        expected_uid += 1

    with pytest.raises(PermissionError, match="source is unsafe") as error:
        load_slot_redaction_values(root, 1, expected_uid=expected_uid, expected_gid=os.getgid())

    assert str(error.value) == "slot diagnostic redaction source is unsafe"


def test_recover_retires_a_restarted_slot_and_clears_its_failed_unit_identity() -> None:
    """The residual ADR-0657 discloses: the fence clears but `require_inactive` still refuses.

    `stop` already retires the retained binding of an out-of-band-restarted slot (#2485), and
    leaves the unit `failed` holding the successor's InvocationID because `systemctl stop` is a
    no-op on it. `recover` is the call that finishes the job in one request.
    """
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(
        1, "empty", invocation_id="f" * 32, result="exit-code", status=1
    )
    runtime.unstoppable.add(started.unit)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert response.ok and response.code == "ok"
    _assert_retained_binding_retired(authority, started)
    assert stores[0].state is None
    assert not stores[0].environment and not stores[0].credential and not stores[0].release
    assert runtime.resets == [started.unit]
    assert started.unit not in runtime.current
    assert _mutations(events)[-3:] == [
        "systemd:stop:kdive-live-worker@1.service",
        "state:cleanup",
        "systemd:reset-failed:kdive-live-worker@1.service",
    ]
    assert [(result.slot, result.phase, result.code) for result in response.slots] == [
        (1, SlotPhase.TERMINATED, "ok")
    ]


def test_recover_clears_a_failed_unit_that_holds_no_fence() -> None:
    """A slot with no state.json AND no active row has nothing to release -- only the unit."""
    stores, runtime, authority, clock, events = _fleet()
    unit = "kdive-live-worker@1.service"
    runtime.current[unit] = _observation(
        1, "empty", invocation_id="f" * 32, result="exit-code", status=1
    )
    runtime.unstoppable.add(unit)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert response.ok
    assert runtime.resets == [unit]
    assert unit not in runtime.current
    assert authority.released == [] and authority.terminations == []
    assert _mutations(events) == [f"systemd:reset-failed:{unit}"]
    assert [(result.slot, result.phase, result.message) for result in response.slots] == [
        (1, None, "cleared the retained unit identity")
    ]


def test_recover_refuses_a_live_slot_without_touching_its_facts_or_fence() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: started})

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok
    assert response.code == "conflict" and response.retry_action == "operator_recovery"
    assert response.message == "recovery refused one or more fixed worker slots"
    assert [(result.slot, result.code, result.message) for result in response.slots] == [
        (1, "recovery_refused", "fixed worker unit still has live processes")
    ]
    assert stores[0].state == started
    assert authority.terminations == []
    assert runtime.resets == [] and runtime.stopped == [] and runtime.signaled == []
    assert _mutations(events) == []


def test_recover_retires_dead_slots_while_refusing_the_live_one() -> None:
    """A refusal is per slot: one running worker must not strand every wedged slot."""
    live = _state(1, SlotPhase.STARTED)
    wedged = _state(2, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: live, 2: wedged})
    runtime.current[wedged.unit] = _observation(
        2, "empty", invocation_id="f" * 32, result="exit-code", status=1
    )
    runtime.unstoppable.add(wedged.unit)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok and response.code == "conflict"
    assert [(result.slot, result.code) for result in response.slots] == [
        (1, "recovery_refused"),
        (2, "ok"),
    ]
    assert stores[0].state == live
    assert stores[1].state is None
    _assert_retained_binding_retired(authority, wedged)
    assert runtime.resets == [wedged.unit]


def test_recover_discards_a_prepared_slot_without_publishing_evidence() -> None:
    """A prepared generation is registered only at gated->registered, so it holds no fence."""
    prepared = _state(1, SlotPhase.PREPARED)
    stores, runtime, authority, clock, events = _fleet(states={1: prepared})
    runtime.current[prepared.unit] = _observation(
        1, "empty", invocation_id="f" * 32, result="exit-code", status=1
    )
    runtime.unstoppable.add(prepared.unit)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert response.ok
    assert stores[0].state is None
    assert authority.registered == set() and authority.terminations == []
    assert _mutations(events) == [
        "state:discard-prepared",
        "systemd:reset-failed:kdive-live-worker@1.service",
    ]
    assert [(result.slot, result.phase) for result in response.slots] == [(1, SlotPhase.PREPARED)]


def test_recover_cleans_a_slot_that_already_carries_terminal_evidence() -> None:
    """Evidence was published before the crash; recovery republishes none of it."""
    terminated = _state(1, SlotPhase.TERMINATED, outcome="killed")
    stores, runtime, authority, clock, events = _fleet(states={1: terminated})
    runtime.current.pop(terminated.unit, None)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert response.ok
    assert stores[0].state is None
    assert authority.terminations == []
    # No `reset-failed`: systemd already reports the unit inactive with an empty identity, which
    # is exactly what the next `require_inactive` wants.
    assert _mutations(events) == ["systemd:stop:kdive-live-worker@1.service", "state:cleanup"]
    assert runtime.resets == []


def test_recover_leaves_an_empty_fleet_untouched() -> None:
    stores, runtime, authority, clock, events = _fleet()

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert response.ok and response.slots == ()
    # The authority probe still runs for each slot; nothing else does, which is what "untouched"
    # means here -- recovery read the fence table and mutated nothing.
    assert runtime.resets == [] and _mutations(events) == []
    assert authority.released == [] and authority.terminations == []


def test_recover_refuses_a_slot_whose_cgroup_membership_is_unreadable() -> None:
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "unknown")

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok
    assert response.code == "dependency_unavailable"
    assert response.retry_action == "restore_systemd"
    assert stores[0].state == started
    assert authority.terminations == [] and runtime.resets == []


def test_recover_refuses_a_slot_whose_invocation_identity_is_unreadable() -> None:
    """ADR-0657 forbids running for a slot whose invocation identity is unreadable.

    #2533 keeps the refusal and changes only how it is reported. It used to escape as
    `SystemdUnavailable`, ending the whole sweep with `dependency_unavailable` -- the same code a
    systemd outage gives, so an operator could not tell the two apart and the other seven slots
    went unrecovered. It is now a per-slot disposition an operator and a log filter can grep.
    """
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _boot_observation(1)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok
    assert response.code == "conflict" and response.retry_action == "operator_recovery"
    assert [(result.slot, result.code, result.message) for result in response.slots] == [
        (
            1,
            "recovery_refused_unreadable_identity",
            "registered invocation identity is unreadable; ADR-0657 forbids recovering it",
        )
    ]
    assert stores[0].state == started
    assert stores[0].discards == 0
    assert authority.terminations == [] and authority.released == []
    assert runtime.resets == []


def test_recover_recovers_a_rejected_binding_through_the_registered_row() -> None:
    """#2533 case 4: the evidenced path is rejected, so the row's own binding is used instead.

    This pinned `evidence_rejected` with the slot untouched while #2533 was open. The retained
    binding no longer matches the row, so `PostgresAuthority.terminate` is refused; recovery now
    falls back to the row the accessor names and releases it with the binding it actually stores.
    """
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "empty", invocation_id="f" * 32)
    authority.reject_termination = True
    # The row's stored binding names the invocation the fence actually claims, which is not the
    # drifted one the slot retained.
    row = _row(1, generation=started.generation, invocation_id="1" * 32)
    authority.rows[started.unit] = [row]

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert response.ok, response.message
    assert authority.released == [(row.incarnation, "killed")]
    assert stores[0].state is None and stores[0].discards == 1
    assert authority.rows[started.unit] == []
    assert runtime.resets == [started.unit]
    assert [(result.slot, result.message) for result in response.slots] == [
        (1, "retired the residual worker slot")
    ]


def test_recover_publishes_the_outcome_the_retained_invocation_itself_reports() -> None:
    """The ordinary recovery: a worker that crashed in place, not an out-of-band restart.

    Here the retained invocation is the one systemd still reports, so the outcome is derived
    from its own `Result` and `ExecMainStatus` rather than taking ADR-0657's fixed `killed` for
    a successor. This is the one branch where recovery attributes observed exit facts to the
    retained incarnation, and they are its own facts -- which is exactly the line ADR-0657 draws.
    """
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(
        1, "empty", invocation_id=started.invocation_id, result="exit-code", status=1
    )
    runtime.unstoppable.add(started.unit)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert response.ok
    assert authority.terminations == [(started.incarnation, "failed")]
    assert authority.terminated_bindings == [
        (started.incarnation, started.boot_id, started.invocation_id)
    ]
    assert stores[0].state is None
    assert runtime.resets == [started.unit]


def test_recover_reports_slots_it_already_retired_when_a_later_slot_fails() -> None:
    """Recovery is the operator escape hatch, so an abort must not hide the fences it released."""
    first = _state(1, SlotPhase.STARTED)
    second = _state(2, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: first, 2: second})
    runtime.current[first.unit] = _observation(
        1, "empty", invocation_id="f" * 32, result="exit-code", status=1
    )
    runtime.unstoppable.add(first.unit)
    runtime.observe_failures[second.unit] = SystemdUnavailable("systemctl show is unavailable")

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok and response.code == "dependency_unavailable"
    _assert_retained_binding_retired(authority, first)
    assert stores[0].state is None
    # Slot 1 no longer loads, so only the carried result can report that it was retired.
    assert [(result.slot, result.phase) for result in response.slots] == [
        (1, SlotPhase.TERMINATED),
        (2, SlotPhase.STARTED),
    ]


def test_recover_clips_its_systemd_work_to_the_same_ceiling_stop_uses() -> None:
    """One slow unit must not consume the whole request and strand the other seven slots."""
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    runtime.current[started.unit] = _observation(1, "empty", invocation_id="f" * 32)

    _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock, 1_000.0)))

    budgets = [
        deadline.remaining()
        for operation, deadline in runtime.systemd_deadlines
        if operation in {"observe", "stop-retained", "reset-failed"}
    ]
    assert budgets and all(budget <= 45.0 for budget in budgets), budgets


def test_recover_keeps_a_refusal_visible_when_a_later_slot_fails() -> None:
    """A refusal's slot is deliberately left loadable, so the reload must not overwrite it.

    "this unit has live processes" is the one fact that changes what the operator does next;
    replacing it with whatever code the sweep later failed on would send them after the wrong
    problem.
    """
    live = _state(1, SlotPhase.STARTED)
    later = _state(2, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: live, 2: later})
    runtime.observe_failures[later.unit] = SystemdUnavailable("systemctl show is unavailable")

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok and response.code == "dependency_unavailable"
    assert [(result.slot, result.code) for result in response.slots] == [
        (1, "recovery_refused"),
        (2, "dependency_unavailable"),
    ]
    assert stores[0].state == live and authority.terminations == []


@pytest.mark.parametrize(
    "observation_factory",
    [
        pytest.param(lambda: _observation(2, "empty"), id="foreign-unit"),
        pytest.param(lambda: _observation(1, "empty", boot_id="other-boot"), id="boot-mismatch"),
        pytest.param(lambda: _boot_observation(1), id="absent-on-retained-boot"),
        pytest.param(lambda: _boot_observation(1, boot_id="other-boot"), id="absent-other-boot"),
        pytest.param(
            lambda: _observation(1, "empty", invocation_id="f" * 32), id="successor-invocation"
        ),
        pytest.param(lambda: _observation(1, "unknown"), id="unknown-membership"),
        pytest.param(lambda: _observation(1, "populated"), id="populated-membership"),
        pytest.param(lambda: _observation(1, "empty", result="success"), id="result-success"),
        pytest.param(
            lambda: _observation(1, "empty", result="exit-code", status=1), id="result-exit-code"
        ),
        pytest.param(lambda: _observation(1, "empty", result="signal"), id="result-signal"),
        pytest.param(lambda: _observation(1, "empty", result="oom-kill"), id="result-oom"),
        pytest.param(lambda: _observation(1, "empty", result="watchdog"), id="result-watchdog"),
    ],
)
def test_identity_outcome_matches_the_state_rules(observation_factory) -> None:
    """The extraction must preserve every rule `_terminal_observation` applied (#2533 Task 3)."""
    state = _state(1, SlotPhase.STARTED, invocation_id="1" * 32)
    identity = lifecycle._InvocationIdentity(
        state.unit, state.slot, cast(str, state.boot_id), cast(str, state.invocation_id)
    )
    observation = observation_factory()

    def _via_state():
        return lifecycle._terminal_observation(state, observation)

    def _via_identity():
        return lifecycle._identity_outcome(identity, observation)

    try:
        expected = _via_state()
    except Exception as exc:  # noqa: BLE001 - the raised type is the thing under comparison
        with pytest.raises(type(exc), match=re.escape(str(exc))):
            _via_identity()
    else:
        assert _via_identity() == expected


def test_state_identity_is_none_only_for_an_unbound_phase() -> None:
    """`_terminal_observation` keeps raising its own conflict for a prepared slot."""
    prepared = _state(1, SlotPhase.PREPARED)
    assert lifecycle._state_identity(prepared) is None
    assert lifecycle._state_identity(_state(1, SlotPhase.STARTED)) is not None

    with pytest.raises(LifecycleConflict, match="bound lifecycle phase has no exact invocation"):
        lifecycle._terminal_observation(prepared, _observation(1, "empty"))


def _residual_fleet(*, document: str | None, drifted: bool = False, rejected: bool = False):
    """Build one slot in a named #2533 residual case, with its fence row still active."""
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, events = _fleet(states={1: started})
    stores[0].state_document = document
    if document is None:
        stores[0].state = None
    runtime.current[started.unit] = _observation(
        1, "empty", invocation_id="f" * 32, result="exit-code", status=1
    )
    runtime.unstoppable.add(started.unit)
    authority.rows[started.unit] = [
        _row(
            1,
            generation=started.generation,
            invocation_id="9" * 32 if drifted else cast(str, started.invocation_id),
        )
    ]
    authority.reject_termination = rejected
    return started, stores, runtime, authority, clock, events


def _assert_residual_slot_retired(response, stores, authority, started) -> None:
    assert response.ok, response.message
    assert authority.released == [(f"local-systemd:{started.unit}:{started.generation}", "killed")]
    assert authority.rows[started.unit] == []
    assert stores[0].state is None and stores[0].state_document is None
    assert not stores[0].environment and not stores[0].credential and not stores[0].release
    assert [(result.slot, result.message) for result in response.slots] == [
        (1, "retired the residual worker slot")
    ]


def test_recover_retires_a_residual_slot_with_no_state_document() -> None:
    """#2533 case 1: SlotStorage.load returns None, so nothing ever reached the fence."""
    started, stores, runtime, authority, clock, _ = _residual_fleet(document=None)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    _assert_residual_slot_retired(response, stores, authority, started)
    assert runtime.resets == [started.unit]


def test_recover_retires_a_residual_slot_with_an_unreadable_state_document() -> None:
    """#2533 case 2: SlotStorage.load raises StateConflict, which wedged the whole sweep."""
    started, stores, runtime, authority, clock, _ = _residual_fleet(document="unreadable")

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    _assert_residual_slot_retired(response, stores, authority, started)
    assert runtime.resets == [started.unit]


def test_recover_retires_a_residual_slot_whose_retained_binding_drifted() -> None:
    """#2533 case 3: the row's binding is authoritative, so death is proven against it."""
    started, stores, runtime, authority, clock, _ = _residual_fleet(document=None, drifted=True)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    _assert_residual_slot_retired(response, stores, authority, started)


def test_recover_retires_a_residual_slot_whose_evidence_the_authority_rejected() -> None:
    """#2533 case 4: the evidenced path is refused, so the registered row is used instead."""
    started, stores, runtime, authority, clock, _ = _residual_fleet(document="valid", rejected=True)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    _assert_residual_slot_retired(response, stores, authority, started)


def test_recover_refuses_a_slot_whose_registered_invocation_is_absent_on_the_retained_boot() -> (
    None
):
    """#2533 case 5, refused per slot -- and the sweep still finishes the slots it can."""
    started, stores, runtime, authority, clock, _ = _residual_fleet(document=None)
    runtime.current[started.unit] = _boot_observation(1)
    second = _state(2, SlotPhase.STARTED)
    stores[1].state = second
    stores[1].state_document = "valid"
    authority.registered.add(second.incarnation)
    runtime.current[second.unit] = _observation(
        2, "empty", invocation_id="f" * 32, result="exit-code", status=1
    )

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok and response.code == "conflict"
    reported = {result.slot: (result.code, result.message) for result in response.slots}
    assert reported[1] == (
        "recovery_refused_unreadable_identity",
        "registered invocation identity is unreadable; ADR-0657 forbids recovering it",
    )
    # Refused: nothing on slot 1 was released or removed.
    assert authority.rows[started.unit] != [] and authority.released == []
    assert stores[0].discards == 0
    # The sweep continued: slot 2 was retired in the same call, evidence published and files
    # cleared. Before #2533 the case-5 raise ended the sweep and slot 2 was never reached.
    assert reported[2][0] == "ok"
    assert (second.incarnation, "killed") in authority.terminations
    assert stores[1].state is None


def test_recover_refuses_a_live_unit_before_reading_the_slot_or_the_fence() -> None:
    """Criterion 3: the populated-cgroup guard is what stops a live worker, and it runs first.

    An earlier version of this test parametrized five residual faults against a populated cgroup
    and asserted nothing happened. Every arm refused at this one guard without reaching the
    residual path at all, so the parameters were dead and the five arms were one test wearing
    five names. What actually needs proving is that the guard precedes every read: no slot
    inspection, and no fence probe.
    """
    started, stores, runtime, authority, clock, _ = _residual_fleet(document=None)
    runtime.current[started.unit] = _observation(1, "populated")
    stores[0].load_calls = 0

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok and response.code == "conflict"
    assert [(result.slot, result.code) for result in response.slots] == [(1, "recovery_refused")]
    # The guard ran before anything read this slot or its fence rows. The other seven slots are
    # still probed, which is why this asserts on slot 1's unit rather than on the fleet's events.
    assert stores[0].load_calls == 0
    assert started.unit not in authority.probed_units
    assert authority.released == [] and authority.terminations == []
    assert authority.rows[started.unit] != []
    assert stores[0].discards == 0
    assert runtime.resets == []


def test_residual_retirement_refuses_a_row_whose_invocation_is_still_live() -> None:
    """The residual path's own live-worker backstop, which `recover` never lets it reach.

    `_recover_slot` refuses a populated cgroup before dispatching, so this branch is defence in
    depth against a future reordering. It is exercised directly rather than left unproven: a
    reordering that removed the outer guard would otherwise land with nothing failing.
    """
    started, stores, runtime, authority, clock, _ = _residual_fleet(document=None)
    coordinator = _coordinator(stores, runtime, authority, clock)
    live = _observation(1, "populated", invocation_id=cast(str, started.invocation_id))
    inspection = stores[0].inspect()

    recovery = asyncio.run(
        coordinator._retire_residual_slot(
            stores[0], inspection, live, _deadline(clock), _deadline(clock)
        )
    )

    assert recovery.refusal == "recovery_refused"
    assert not recovery.cleared
    assert authority.released == []
    assert authority.rows[started.unit] != []
    assert stores[0].discards == 0


def test_recover_refuses_a_slot_whose_row_was_registered_by_another_host() -> None:
    """A foreign-host fence must not be released -- and must not let this slot's files be cleared.

    The incarnation prefix carries no host, so a shared database can surface another host's row
    over this slot's unit. Skipping that row and carrying on would delete this slot's files while
    that fence is still held, inverting the ordering the failure model requires. The slot is
    refused whole instead, with the code an operator reconciles rather than reboots.
    """
    started, stores, runtime, authority, clock, _ = _residual_fleet(document=None)
    stores[0].environment = True
    stores[0].credential = True
    stores[0].release = True
    authority.rows[started.unit] = [_row(1, generation=started.generation, host="some-other-host")]

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok and response.code == "conflict"
    assert [(result.slot, result.code) for result in response.slots] == [
        (1, "recovery_refused_incoherent_row")
    ]
    assert authority.released == []
    assert authority.rows[started.unit] != []
    # The point of the finding: the files are still here.
    assert stores[0].discards == 0
    assert stores[0].environment and stores[0].credential and stores[0].release


def test_recover_refuses_a_slot_before_releasing_any_of_its_rows() -> None:
    """A slot with one unrecoverable row is refused whole, never left half-released."""
    started, stores, runtime, authority, clock, _ = _residual_fleet(document=None)
    authority.rows[started.unit] = [
        _row(1, generation=started.generation),
        # The second row's binding names a different unit, so it cannot be trusted to name an
        # invocation and the whole slot is refused.
        _row(1, generation="e" * 32, unit="kdive-live-worker@7.service"),
    ]

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok and response.code == "conflict"
    assert [(result.slot, result.code) for result in response.slots] == [
        (1, "recovery_refused_incoherent_row")
    ]
    assert authority.released == []
    assert len(authority.rows[started.unit]) == 2
    assert stores[0].discards == 0


def test_recover_residual_support_does_not_move_the_protocol_identity() -> None:
    """#2533 adds no Operation value and no request or response field, so no reprovision."""
    from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
        lifecycle_protocol_identity,
    )

    assert lifecycle_protocol_identity() == (
        "1:d5de155830bd087207ab73060df513bba91fe4d57436615b4cf7b6359d594a5b"
    )


def test_recover_sweeps_a_stale_row_left_beside_the_retained_generation() -> None:
    """A slot can hold an older active row than the one its state.json names.

    `prepare` mints a fresh generation for a slot whose files were lost out of band and registers
    it beside the row still held, so the evidenced path retires one and leaves the other. Reporting
    that slot `ok` would leave it half-released, which the failure model forbids.
    """
    started = _state(1, SlotPhase.STARTED)
    stores, runtime, authority, clock, _ = _fleet(states={1: started})
    stores[0].state_document = "valid"
    runtime.current[started.unit] = _observation(
        1, "empty", invocation_id="f" * 32, result="exit-code", status=1
    )
    stale = _row(1, generation="c" * 32)
    authority.rows[started.unit] = [
        _row(1, generation=started.generation, invocation_id=cast(str, started.invocation_id)),
        stale,
    ]

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert response.ok, response.message
    # The retained generation went through the evidenced path...
    assert (started.incarnation, "killed") in authority.terminations
    # ...and the stale row was swept in the same call rather than left held.
    assert (stale.incarnation, "killed") in authority.released
    assert authority.rows[started.unit] == []
    assert [(result.slot, result.phase) for result in response.slots] == [(1, SlotPhase.TERMINATED)]


def test_recover_refuses_the_sweep_while_unmanaged_workers_run() -> None:
    """Cgroup membership cannot see a `kdive worker` outside every fixed unit.

    Recovery releases fences, so it takes the same guard `start` takes rather than resting on the
    weaker of the two liveness checks.
    """
    started, stores, runtime, authority, clock, _ = _residual_fleet(document=None)
    runtime.unmanaged = (UnmanagedWorker(pid=4321, uid=1000),)

    response = _run(_coordinator(stores, runtime, authority, clock).recover(_deadline(clock)))

    assert not response.ok and response.code == "conflict"
    assert response.retry_action == "operator_recovery"
    assert authority.released == [] and authority.terminations == []
    assert authority.rows[started.unit] != []
    assert authority.probed_units == []
    assert stores[0].discards == 0
    assert runtime.resets == []
