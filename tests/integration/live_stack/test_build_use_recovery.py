"""Unit tests of the stranded build-use pin frame (#3111; no stack needed)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
    LifecycleResponse,
    SlotPhase,
    SlotResult,
)
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack import build_use_recovery as recovery
from tests.integration.live_stack.build_use_recovery import Seams, Stranded
from tests.integration.live_stack.scenario import ScenarioStop

_UNIT = "kdive-live-worker@2.service"
_HOLDER = "local:holder-2"
_PIN = {"use_id": "u1", "holder_worker_id": _HOLDER, "investigation_id": "i1"}


@pytest.fixture(autouse=True)
def _no_restore_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restore that would keep polling fails at once instead of after five minutes."""
    monkeypatch.setattr(recovery, "_RESTORE_DEADLINE_S", 0.0)


def _response(*phases: SlotPhase) -> LifecycleResponse:
    slots = tuple(
        SlotResult(slot=n, unit=f"kdive-live-worker@{n}.service", phase=phase)
        for n, phase in enumerate(phases, start=1)
    )
    return LifecycleResponse(ok=True, code="ok", message="", retry_action="none", slots=slots)


@dataclass
class _Host:
    """Fake host and database: ``pins`` is what each pin read returns, in order."""

    pins: list[list[dict[str, Any]]]
    lock_free: bool = True
    incarnation_state: str = "active"
    unit_states: list[str] = field(default_factory=lambda: ["failed"])
    job_states: list[str] = field(default_factory=lambda: ["succeeded"])
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def sudo(self, *argv: str) -> None:
        self.calls.append(("sudo", *argv))

    def lifecycle(self, *argv: str) -> LifecycleResponse:
        self.calls.append(("lifecycle", *argv))
        if argv[0] == "status":
            self.incarnation_state = "terminated"
        return _response(SlotPhase.STARTED, SlotPhase.STARTED)

    def active_state(self, unit: str) -> str:
        return self.unit_states.pop(0) if len(self.unit_states) > 1 else self.unit_states[0]

    async def rows(self, _db: str, query: str, _params: Sequence[object]) -> list[dict[str, Any]]:
        if query == recovery.PINS:
            return self.pins.pop(0) if len(self.pins) > 1 else self.pins[0]
        if query == recovery.INCARNATION:
            outcome = "killed" if self.incarnation_state == "terminated" else None
            binding = {"unit": _UNIT}
            return [
                {"state": self.incarnation_state, "outcome": outcome, "authority_binding": binding}
            ]
        state = self.job_states.pop(0) if len(self.job_states) > 1 else self.job_states[0]
        return [{"state": state}]

    async def lock(self, _db: str, holder: str) -> bool:
        self.calls.append(("lock", holder))
        return self.lock_free

    async def sleep(self, _seconds: float) -> None:
        return None

    def seams(self) -> Seams:
        return Seams(self.sudo, self.lifecycle, self.active_state, self.rows, self.lock, self.sleep)


def _freeze(host: _Host) -> tuple[dict[str, Any], str]:
    return asyncio.run(recovery.freeze_holder("db", "i1", host.seams()))


def test_freeze_holds_a_pin_outside_its_release_transaction() -> None:
    host = _Host(pins=[[], [_PIN], [_PIN]])
    assert _freeze(host) == (_PIN, _UNIT)
    assert host.calls == [("sudo", "systemctl", "freeze", _UNIT), ("lock", _HOLDER)]


@pytest.mark.parametrize(
    ("pins", "lock_free"),
    [([[_PIN], []], True), ([[_PIN], [_PIN]], False)],
    ids=["pin-released", "release-transaction-open"],
)
def test_a_lost_race_thaws_the_holder_and_stops_blocked(
    pins: list[list[dict[str, Any]]], lock_free: bool
) -> None:
    host = _Host(pins=pins, lock_free=lock_free)
    with pytest.raises(ScenarioStop) as stop:
        _freeze(host)
    assert stop.value.outcome is Outcome.BLOCKED
    assert host.calls[0] == ("sudo", "systemctl", "freeze", _UNIT)
    assert host.calls[-1] == ("sudo", "systemctl", "thaw", _UNIT)


def test_kill_waits_for_death_before_the_witness_publishes_termination() -> None:
    host = _Host(pins=[[_PIN]], unit_states=["active", "deactivating", "failed"])
    stranded = Stranded("db", "p", _PIN, _UNIT, host.seams())
    incarnation = asyncio.run(stranded.kill())
    assert incarnation["state"] == "terminated"
    assert host.calls == [
        ("sudo", "systemctl", "kill", "--signal=SIGKILL", _UNIT),
        ("lifecycle", "status"),
    ]
    assert host.unit_states == ["failed"]


def _restore(host: _Host, stranded: Stranded | None, cancels: list[str]) -> str | None:
    async def cancel() -> None:
        cancels.append("cancel")
        host.calls.append(("cancel",))

    async def recover(pinned: Stranded) -> None:
        host.calls.append(("recover-pin", pinned.holder))
        host.pins = [[]]

    return asyncio.run(
        recovery.restore(
            host.seams(),
            "db",
            stranded,
            cancel=cancel,
            recover=recover,
            job_id="j1",
            investigation="i1",
            slots=2,
        )
    )


def test_restore_cancels_a_killed_holders_job_before_restarting_the_fleet() -> None:
    host = _Host(pins=[[]], job_states=["canceled"])
    stranded = Stranded("db", "p", _PIN, _UNIT, host.seams(), killed=True)
    assert _restore(host, stranded, []) == "canceled"
    # Only `recover` resets the killed unit's failed identity; `start` alone would refuse it.
    assert host.calls == [
        ("cancel",),
        ("lifecycle", "stop"),
        ("lifecycle", "recover"),
        ("lifecycle", "start", "2"),
    ]


def test_restore_recovers_a_pin_the_body_left_after_the_kill() -> None:
    host = _Host(pins=[[_PIN]], job_states=["canceled"], incarnation_state="terminated")
    stranded = Stranded("db", "p", _PIN, _UNIT, host.seams(), killed=True)
    assert _restore(host, stranded, []) == "canceled"
    assert host.calls[:2] == [("recover-pin", _HOLDER), ("cancel",)]


def test_restore_names_an_orphan_whose_holder_was_never_witnessed() -> None:
    host = _Host(pins=[[_PIN]], job_states=["canceled"])
    stranded = Stranded("db", "p", _PIN, _UNIT, host.seams(), killed=True)
    with pytest.raises(AssertionError, match="orphaned and its holder is active"):
        _restore(host, stranded, [])
    assert ("cancel",) not in host.calls


def test_restore_thaws_a_live_holder_and_waits_for_its_own_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recovery, "_RESTORE_DEADLINE_S", 60.0)
    host = _Host(pins=[[_PIN], []], job_states=["running", "succeeded"])
    stranded = Stranded("db", "p", _PIN, _UNIT, host.seams())
    cancels: list[str] = []
    assert _restore(host, stranded, cancels) == "succeeded"
    # A job canceled under a live holder would refuse its release and strand the pin.
    assert not cancels
    assert host.calls == [("sudo", "systemctl", "thaw", _UNIT)]


def test_restore_fails_when_a_pin_outlives_the_job() -> None:
    host = _Host(pins=[[_PIN]], job_states=["canceled"])
    with pytest.raises(AssertionError, match="1 pin"):
        _restore(host, None, [])
