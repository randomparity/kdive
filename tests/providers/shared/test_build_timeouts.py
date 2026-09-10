"""Pin the shared provider build subprocess timeout and its worker-host scaling (ADR-0637)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import kdive.config as config
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER
from kdive.providers.shared import build_timeouts
from kdive.providers.shared.build_timeouts import slow_build_tool_timeout_s


@pytest.fixture(autouse=True)
def _isolate_config() -> Iterator[None]:
    yield
    config.reset()


def test_slow_build_tool_timeout_is_thirty_minutes() -> None:
    assert build_timeouts.SLOW_BUILD_TOOL_TIMEOUT_S == 30 * 60
    assert build_timeouts.SLOW_BUILD_TOOL_TIMEOUT_S == 1800


# --- worker-host scaling (#2397, ADR-0637) ----------------------------------------------------
#
# Keyed off the WORKER HOST's KVM, not a System's accel: the rootfs build tools drive a libguestfs
# appliance, which is a host-arch VM the worker boots on its own host. ``kvm_present`` is injected
# so both branches are proven without a real ``/dev/kvm`` — otherwise the answer would be a
# property of whichever machine ran the suite.


def test_host_with_kvm_keeps_the_unscaled_budget() -> None:
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "10.0"})
    assert slow_build_tool_timeout_s(kvm_present=lambda: True) == 1800


def test_host_without_kvm_scales_by_the_configured_multiplier() -> None:
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "7.0"})
    assert slow_build_tool_timeout_s(kvm_present=lambda: False) == 1800 * 7


def test_host_without_kvm_uses_the_setting_default() -> None:
    config.load({})  # setting default (10.0)
    assert slow_build_tool_timeout_s(kvm_present=lambda: False) == 18000


def test_both_branches_return_int_for_the_subprocess_timeout_contract() -> None:
    # run_guestfs_tool takes `timeout_s: int` and echoes it into the timeout error's details
    # payload; a float would change both the signature and the reported value.
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "2.5"})
    assert isinstance(slow_build_tool_timeout_s(kvm_present=lambda: True), int)
    scaled = slow_build_tool_timeout_s(kvm_present=lambda: False)
    assert isinstance(scaled, int)
    assert scaled == 4500


def test_kvm_never_reads_config() -> None:
    # A malformed multiplier must not fail a build on a KVM host: the KVM branch short-circuits
    # before touching configuration, mirroring tcg_deadline_multiplier (ADR-0341).
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "not-a-float"})
    assert slow_build_tool_timeout_s(kvm_present=lambda: True) == 1800


def test_clears_the_measured_emulated_repack_failure() -> None:
    # The regression this scaling exists for: in-guest build-fs on an emulated-POWER host failed
    # at `virt-tar-out exceeded its timeout {'timeout_s': 1800}` (#2383, deviation 1).
    config.load({})
    assert slow_build_tool_timeout_s(kvm_present=lambda: False) > 1800


def test_default_probe_is_the_worker_host_kvm_node() -> None:
    # Without an injected probe the budget follows the worker host's own /dev/kvm (ADR-0352),
    # resolved per call rather than bound at import.
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "10.0"})
    assert slow_build_tool_timeout_s() in (1800, 18000)
