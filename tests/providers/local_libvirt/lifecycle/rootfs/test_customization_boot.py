"""Tests for the customization-boot console classifier (ADR-0345)."""

from __future__ import annotations

from kdive.providers.local_libvirt.lifecycle.rootfs.customization_boot import (
    CustomizeVerdict,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.customization_boot import (
    classify_customization_console as C,
)


def test_ok_marker_wins():
    assert C(b"...\nkdive-customize-ok\n") is CustomizeVerdict.OK


def test_fail_marker():
    assert C(b"dnf: No match\nkdive-customize-failed\n") is CustomizeVerdict.FAILED


def test_genuine_oops_fails():
    assert C(b"Oops: 0000 [#1] SMP\n") is CustomizeVerdict.FAILED


def test_benign_tcg_stall_is_pending():
    assert C(b"rcu: INFO: rcu_sched detected stalls on CPUs\n") is CustomizeVerdict.PENDING
    assert C(b"watchdog: BUG: soft lockup - CPU#0 stuck for 22s!\n") is CustomizeVerdict.PENDING


def test_real_bug_still_fails():
    assert C(b"BUG: unable to handle kernel paging request\n") is CustomizeVerdict.FAILED


def test_pending_when_quiet():
    assert C(b"[  ok  ] Started systemd-logind\n") is CustomizeVerdict.PENDING
