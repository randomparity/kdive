"""The shared readiness poll loop behind runs.boot and the provision first-boot wait (ADR-0680)."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from kdive.providers.local_libvirt.lifecycle.boot.readiness import (
    ProbeFailure,
    ReadinessResult,
    poll_readiness,
)

_SYS = UUID("11111111-1111-1111-1111-111111111111")


def _seq(*results: ReadinessResult) -> tuple[Callable[[UUID], ReadinessResult], list[UUID]]:
    it = iter(results)
    calls: list[UUID] = []

    def probe(system_id: UUID) -> ReadinessResult:
        calls.append(system_id)
        return next(it)

    return probe, calls


def test_returns_first_answer() -> None:
    probe, calls = _seq(ReadinessResult(False, False), ReadinessResult(True, True))
    outcome = poll_readiness(probe, _SYS, 5)
    assert outcome.result == ReadinessResult(True, True)
    assert len(calls) == 2


def test_keeps_first_probe_error() -> None:
    probe, _ = _seq(
        ReadinessResult(False, False, ProbeFailure.VIRSH_TIMEOUT),
        ReadinessResult(False, False, ProbeFailure.VIRSH_MISSING),
        ReadinessResult(True, False, crash_signature="Kernel panic"),
    )
    outcome = poll_readiness(probe, _SYS, 5)
    assert outcome.first_probe_error is ProbeFailure.VIRSH_TIMEOUT
    assert outcome.result is not None
    assert outcome.result.crash_signature == "Kernel panic"


def test_exhaustion_returns_none() -> None:
    probe, calls = _seq(*[ReadinessResult(False, False)] * 3)
    assert poll_readiness(probe, _SYS, 3).result is None
    assert len(calls) == 3


def test_deadline_stops_before_the_poll_count() -> None:
    now = [0.0]

    def probe(_system_id: UUID) -> ReadinessResult:
        now[0] += 15.0  # a hung virsh probe plus the poll sleep
        return ReadinessResult(False, False)

    outcome = poll_readiness(probe, _SYS, 100, deadline=45.0, clock=lambda: now[0])
    assert outcome.result is None
    assert now[0] == 45.0
