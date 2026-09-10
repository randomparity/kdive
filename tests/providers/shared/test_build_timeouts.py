"""Pin the shared provider build subprocess timeout and its worker-host scaling (ADR-0637)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

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


# --- the default probe (#2397, ADR-0637) ------------------------------------------------------
#
# The un-injected path is the one every production call site takes, and its probe is this budget's
# own: read+write openability of ${KDIVE_KVM_NODE:-/dev/kvm}, matching preflight-env.sh for the
# same libguestfs appliance rather than ADR-0352's URI-selected presence test. KDIVE_KVM_NODE is
# the seam the shell tier's tests already drive, so no monkeypatching is needed to reach it.


def _openable_node(tmp_path: Path) -> Path:
    node = tmp_path / "kvm"
    node.write_bytes(b"")
    node.chmod(0o600)
    return node


def test_un_injected_call_keeps_the_unscaled_budget_when_the_node_opens(tmp_path: Path) -> None:
    node = _openable_node(tmp_path)
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "10.0", "KDIVE_KVM_NODE": str(node)})
    assert slow_build_tool_timeout_s() == 1800


def test_un_injected_call_scales_when_the_node_is_absent(tmp_path: Path) -> None:
    config.load(
        {LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "10.0", "KDIVE_KVM_NODE": str(tmp_path / "absent")}
    )
    assert slow_build_tool_timeout_s() == 18000


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens any mode, so read-only proves nothing")
def test_a_node_the_worker_uid_cannot_write_scales(tmp_path: Path) -> None:
    # The host class this budget's own probe exists for: the node is present, so ADR-0352's
    # presence test would call it KVM, but the worker uid cannot open it read+write and the
    # appliance is emulated. preflight-env.sh calls the same host fatal.
    node = _openable_node(tmp_path)
    node.chmod(0o400)
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "10.0", "KDIVE_KVM_NODE": str(node)})
    assert slow_build_tool_timeout_s() == 18000


def test_an_empty_node_override_falls_back_to_dev_kvm(monkeypatch: pytest.MonkeyPatch) -> None:
    # ${KDIVE_KVM_NODE:-/dev/kvm} treats empty as unset; an empty string would otherwise be a
    # path os.access always refuses, silently scaling every budget on a KVM host.
    probed: list[str] = []

    def _record(node: str, _mode: int) -> bool:
        probed.append(node)
        return True

    monkeypatch.setattr(build_timeouts.os, "access", _record)
    config.load({"KDIVE_KVM_NODE": ""})
    assert slow_build_tool_timeout_s() == 1800
    assert probed == ["/dev/kvm"]


def test_the_probe_is_resolved_per_call_not_bound_at_import(tmp_path: Path) -> None:
    # Binding the verdict once would outlive the fact it measures: flipping the host's answer
    # between two calls must change the second budget.
    node = _openable_node(tmp_path)
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "10.0", "KDIVE_KVM_NODE": str(node)})
    first = slow_build_tool_timeout_s()
    node.unlink()
    assert [first, slow_build_tool_timeout_s()] == [1800, 18000]
