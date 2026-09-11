"""Pin the shared provider build subprocess timeout and its worker-host scaling (ADR-0637)."""

from __future__ import annotations

import errno
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
# own: whether ${KDIVE_KVM_NODE:-/dev/kvm} actually opens read+write, rather than ADR-0352's
# URI-selected presence test. KDIVE_KVM_NODE is the seam the shell tier's tests already drive, so
# most of these reach the probe with no monkeypatching at all.
#
# The probe opens rather than stat-ing because permission bits do not answer the question on a
# systemd host: udev's `static_node=kvm` publishes /dev/kvm at 0666 whether or not the module ever
# loads (#2414).


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


def test_a_present_world_writable_node_that_cannot_open_scales(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The static-node host class, measured on the #2383 emulated-POWER host of record (#2414):
    # udev publishes /dev/kvm at 0666 whether or not `kvm` loads, so with the module blacklisted
    # the permission test passed while the open raised ENODEV. Every guest ran under TCG and the
    # appliance was emulated, yet this budget stayed unscaled at 1800 s — the one host class
    # #2397 exists to scale.
    node = _openable_node(tmp_path)
    node.chmod(0o666)
    assert os.access(node, os.R_OK | os.W_OK), "the permission test the old probe ran must pass"

    real_open = build_timeouts.os.open

    def _enodev(path: str, flags: int, *args: int) -> int:
        if str(path) == str(node):
            raise OSError(errno.ENODEV, "No such device")
        return real_open(path, flags, *args)

    monkeypatch.setattr(build_timeouts.os, "open", _enodev)
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "10.0", "KDIVE_KVM_NODE": str(node)})
    assert slow_build_tool_timeout_s() == 18000


def test_the_probe_closes_the_descriptor_it_opens(tmp_path: Path) -> None:
    # A probe runs per call on a long-lived worker, so a leaked descriptor per call is a leak per
    # build tool. Opening the same node many times must not exhaust the process's fd table.
    node = _openable_node(tmp_path)
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "10.0", "KDIVE_KVM_NODE": str(node)})
    before = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    for _ in range(64):
        assert slow_build_tool_timeout_s() == 1800
    assert len(os.listdir(f"/proc/{os.getpid()}/fd")) == before


def test_an_empty_node_override_falls_back_to_dev_kvm(monkeypatch: pytest.MonkeyPatch) -> None:
    # ${KDIVE_KVM_NODE:-/dev/kvm} treats empty as unset; an empty string would otherwise be a
    # path the open always refuses, silently scaling every budget on a KVM host.
    probed: list[str] = []
    real_open = build_timeouts.os.open

    def _record(node: str, flags: int, *args: int) -> int:
        probed.append(str(node))
        return real_open(os.devnull, flags, *args)

    monkeypatch.setattr(build_timeouts.os, "open", _record)
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
