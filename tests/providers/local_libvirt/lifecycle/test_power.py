"""Direct tests of the shared clean power-off helper (ADR-0679, ADR-0681).

The install and external-boot callers cover the full wait/re-send/timeout paths through their own
seams; these pin the helper's own contract at its import path.
"""

from __future__ import annotations

from collections.abc import Sequence

import libvirt
import pytest

from kdive.providers.local_libvirt.lifecycle.power import clean_shutdown_bound_s, power_off
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER


class _Domain:
    def __init__(self, state: int, *, honours: bool = True) -> None:
        self.current = state
        self.honours = honours
        self.calls: list[str] = []

    def state(self, flags: int = 0) -> Sequence[object]:
        del flags
        return [self.current, 0]

    def shutdown(self) -> int:
        self.calls.append("shutdown")
        if self.honours:
            self.current = libvirt.VIR_DOMAIN_SHUTOFF
        return 0

    def destroy(self) -> int:
        self.calls.append("destroy")
        self.current = libvirt.VIR_DOMAIN_SHUTOFF
        return 0


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_bound_is_60_s_on_kvm_and_scaled_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LIBVIRT_TCG_DEADLINE_MULTIPLIER.name, "10.0")
    assert clean_shutdown_bound_s("kvm") == 60.0
    assert clean_shutdown_bound_s(None) == 600.0


def test_power_off_uses_the_bound_it_is_given() -> None:
    domain = _Domain(libvirt.VIR_DOMAIN_RUNNING, honours=False)
    clock = _Clock()
    power_off(domain, "kdive-x", 7.0, clock.sleep, clock)
    assert clock.now == 7.0
    assert domain.calls == ["shutdown", "destroy"]


def test_power_off_stops_a_running_guest_cleanly() -> None:
    domain = _Domain(libvirt.VIR_DOMAIN_RUNNING)
    clock = _Clock()
    power_off(domain, "kdive-x", 60.0, clock.sleep, clock)
    assert domain.calls == ["shutdown"]


def test_power_off_destroys_a_paused_guest_at_once() -> None:
    domain = _Domain(libvirt.VIR_DOMAIN_PAUSED)
    clock = _Clock()
    power_off(domain, "kdive-x", 60.0, clock.sleep, clock)
    assert domain.calls == ["destroy"]
    assert clock.now == 0.0
