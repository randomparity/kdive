"""Unit tests for the kdump-completion poll loop (ADR-0217).

``_real_wait_for_vmcore`` used to force-off the domain immediately, racing the in-guest kdump
and harvesting an empty ``/var/crash``. The fix waits for the guest to self-shut-off (kdump's
``final_action poweroff``) within a bounded window before forcing off and harvesting. The wait
is driven through an injected domain-settled probe and sleep seam so the poll logic is unit
tested with fakes; only the live libvirt probe/sleep wiring is ``live_vm``-gated.
"""

from __future__ import annotations

from kdive.providers.local_libvirt.retrieve import (
    _KDUMP_SETTLE_POLL_INTERVAL_S,
    _KDUMP_SETTLE_TIMEOUT_S,
    _poll_until_settled,
)


def test_returns_true_without_sleeping_when_already_settled() -> None:
    slept: list[float] = []
    settled = _poll_until_settled(
        lambda: True,
        slept.append,
        timeout_s=120.0,
        poll_interval_s=3.0,
    )
    assert settled is True
    assert slept == []  # an already-settled domain is harvested immediately, no premature wait


def test_returns_true_once_domain_settles_after_some_polls() -> None:
    """The guest is still dumping for a few polls, then self-shuts-off → wait succeeds."""
    probe_results = iter([False, False, False, True])
    slept: list[float] = []

    def is_settled() -> bool:
        return next(probe_results)

    settled = _poll_until_settled(
        is_settled,
        slept.append,
        timeout_s=120.0,
        poll_interval_s=3.0,
    )
    assert settled is True
    # slept exactly once between each of the three not-settled probes and the settling probe.
    assert slept == [3.0, 3.0, 3.0]


def test_times_out_when_domain_never_settles() -> None:
    """A guest whose kdump reboots (never self-shuts-off) → bounded timeout, not unbounded."""
    probes = 0
    slept: list[float] = []

    def is_settled() -> bool:
        nonlocal probes
        probes += 1
        return False

    settled = _poll_until_settled(
        is_settled,
        slept.append,
        timeout_s=12.0,
        poll_interval_s=3.0,
    )
    assert settled is False
    # ceil(12/3) == 4 probes, with a sleep between each pair (one fewer than probes).
    assert probes == 4
    assert slept == [3.0, 3.0, 3.0]


def test_total_wait_is_bounded_by_the_timeout() -> None:
    """Even with sub-interval timeouts the probe budget stays >= 1 and never over-waits."""
    slept: list[float] = []
    settled = _poll_until_settled(
        lambda: False,
        slept.append,
        timeout_s=1.0,
        poll_interval_s=3.0,
    )
    assert settled is False
    assert slept == []  # ceil(1/3) == 1 probe, no sleep, total wait 0s <= timeout


def test_settle_window_constants_are_sane() -> None:
    assert _KDUMP_SETTLE_TIMEOUT_S > _KDUMP_SETTLE_POLL_INTERVAL_S > 0
