"""Direct tests of the shared clean power-off helper (ADR-0679, ADR-0681).

The install and external-boot callers cover the full wait/re-send/timeout paths through their own
seams; these pin the helper's own contract at its import path.
"""

from __future__ import annotations

from collections.abc import Sequence

import libvirt
import pytest

from kdive.providers.local_libvirt.lifecycle.power import (
    clean_shutdown_bound_s,
    destroy_or_accept_shutoff,
    power_off,
)
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER
from tests.providers.local_libvirt.fakes import libvirt_error


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


class _RacedDomain(_Domain):
    def __init__(self, state: int, final_state: int, error_code: int) -> None:
        super().__init__(state, honours=False)
        self.final_state = final_state
        self.error_code = error_code

    def destroy(self) -> int:
        self.calls.append("destroy")
        self.current = self.final_state
        raise libvirt_error(self.error_code)


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


@pytest.mark.parametrize("initial", [libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED])
def test_power_off_accepts_shutoff_racing_destroy(initial: int) -> None:
    domain = _RacedDomain(initial, libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_ERR_OPERATION_INVALID)
    clock = _Clock()
    power_off(domain, "kdive-x", 1.0, clock.sleep, clock)
    assert domain.calls[-1] == "destroy"


@pytest.mark.parametrize(
    ("final_state", "error_code"),
    [
        (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_ERR_OPERATION_INVALID),
        (libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_ERR_INTERNAL_ERROR),
    ],
)
def test_power_off_rejects_failed_destroy(final_state: int, error_code: int) -> None:
    domain = _RacedDomain(libvirt.VIR_DOMAIN_PAUSED, final_state, error_code)
    clock = _Clock()
    with pytest.raises(libvirt.libvirtError) as exc:
        power_off(domain, "kdive-x", 1.0, clock.sleep, clock)
    assert exc.value.get_error_code() == error_code


def test_power_off_accepts_race_after_refused_shutdown() -> None:
    class RefusedShutdown(_RacedDomain):
        def shutdown(self) -> int:
            self.calls.append("shutdown")
            raise libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID)

    domain = RefusedShutdown(
        libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_ERR_OPERATION_INVALID
    )
    clock = _Clock()
    power_off(domain, "kdive-x", 1.0, clock.sleep, clock)
    assert domain.calls == ["shutdown", "destroy"]


def test_destroy_race_propagates_failed_state_reread() -> None:
    class UnreadableState(_RacedDomain):
        def state(self, flags: int = 0) -> Sequence[object]:
            raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)

    domain = UnreadableState(
        libvirt.VIR_DOMAIN_PAUSED, libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_ERR_OPERATION_INVALID
    )
    with pytest.raises(libvirt.libvirtError) as exc:
        destroy_or_accept_shutoff(domain)
    assert exc.value.get_error_code() == libvirt.VIR_ERR_INTERNAL_ERROR
