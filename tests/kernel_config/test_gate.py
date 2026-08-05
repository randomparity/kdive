"""Unit tests for the kernel-config gate seams (ADR-0322, ADR-0478).

`debuginfo_warning` and `rootfs_mount_warning` warn, never refuse, and fail open;
`crash_capture_refusal` is the one seam that refuses, and only on its narrow gated subset.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from psycopg import AsyncConnection

from kdive.kernel_config.gate import (
    CRASH_CONFIG_REASON,
    DEBUGINFO_UNLOADABLE_REASON,
    MISSING_BOOT_CONFIG_REASON,
    MISSING_DEBUGINFO_REASON,
    crash_capture_refusal,
    debuginfo_unloadable_warning,
    debuginfo_warning,
    missing_effective_config_nudge,
    rootfs_mount_warning,
)
from kdive.kernel_config.parse import KernelConfig

_RUN_ID = uuid4()
_CONN = cast(AsyncConnection, object())  # the loader is patched, so conn is never used


def _patched_load(config: KernelConfig | None) -> Any:
    async def _fake_load(
        conn: Any, run_id: Any, *, store_factory: Any = None
    ) -> KernelConfig | None:
        return config

    return patch("kdive.kernel_config.gate.load_effective_config", _fake_load)


def _call(*, config: KernelConfig | None, has_uploaded_vmlinux: bool) -> dict[str, Any] | None:
    async def _run() -> dict[str, Any] | None:
        with _patched_load(config):
            return await debuginfo_warning(
                _CONN, _RUN_ID, has_uploaded_vmlinux=has_uploaded_vmlinux
            )

    return asyncio.run(_run())


def test_uploaded_vmlinux_suppresses_warning_without_reading_config():
    # A raising loader proves the vmlinux short-circuit runs before any config read.
    async def _boom(conn: Any, run_id: Any, *, store_factory: Any = None) -> KernelConfig:
        raise AssertionError("load_effective_config must not be called when vmlinux is uploaded")

    async def _run() -> dict[str, Any] | None:
        with patch("kdive.kernel_config.gate.load_effective_config", _boom):
            return await debuginfo_warning(_CONN, _RUN_ID, has_uploaded_vmlinux=True)

    assert asyncio.run(_run()) is None


def test_absent_config_fails_open_to_no_warning():
    assert _call(config=None, has_uploaded_vmlinux=False) is None


def test_config_with_btf_produces_no_warning():
    cfg = KernelConfig(frozenset({"DEBUG_INFO", "DEBUG_INFO_BTF", "DEBUG_KERNEL"}))
    assert _call(config=cfg, has_uploaded_vmlinux=False) is None


def test_config_with_dwarf_but_no_btf_still_warns():
    # In-guest drgn-live reads BTF, not the kernel .config's DWARF (the DWARF vmlinux is not on the
    # guest rootfs). A DWARF-only config with no uploaded vmlinux is still blind, so it must warn.
    cfg = KernelConfig(frozenset({"DEBUG_INFO", "DEBUG_INFO_DWARF5", "DEBUG_KERNEL"}))
    warning = _call(config=cfg, has_uploaded_vmlinux=False)
    assert warning is not None
    assert warning["missing"] == ["DEBUG_INFO_BTF"]


def test_config_lacking_btf_warns_and_names_btf():
    cfg = KernelConfig(frozenset({"DEBUG_INFO", "DEBUG_KERNEL"}))  # no BTF
    warning = _call(config=cfg, has_uploaded_vmlinux=False)
    assert warning is not None
    assert warning["reason"] == MISSING_DEBUGINFO_REASON
    assert warning["missing"] == ["DEBUG_INFO_BTF"]
    assert "vmlinux" in warning["remediation"]


def test_unloadable_warning_is_distinct_reason_naming_btf():
    # The runtime-probe payload (ADR-0329) is a distinct reason from the static gate, but shares the
    # {reason, missing, remediation} shape and keys on the same BTF symbol.
    warning = debuginfo_unloadable_warning()
    assert warning["reason"] == DEBUGINFO_UNLOADABLE_REASON
    assert warning["reason"] != MISSING_DEBUGINFO_REASON
    assert warning["missing"] == ["DEBUG_INFO_BTF"]
    assert "vmlinux" in cast(str, warning["remediation"])


def _rootfs_call(config: KernelConfig | None) -> dict[str, Any] | None:
    async def _run() -> dict[str, Any] | None:
        with _patched_load(config):
            return await rootfs_mount_warning(_CONN, _RUN_ID)

    return asyncio.run(_run())


def test_rootfs_absent_config_fails_open_to_no_warning():
    assert _rootfs_call(None) is None


def test_rootfs_full_boot_set_produces_no_warning():
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}))
    assert _rootfs_call(cfg) is None


def test_rootfs_ext4_local_libvirt_kernel_stays_silent_after_adding_xfs():
    # #1626 regression guard: XFS_FS joined rootfs_mount as an OR-group member, not as a second
    # required clause. Appending it with _plain would have made every ext4-root local-libvirt
    # kernel — the overwhelmingly common case — start emitting a spurious missing_boot_config.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK", "KEXEC", "MAGIC_SYSRQ"}))
    assert _rootfs_call(cfg) is None


def test_rootfs_xfs_only_kernel_is_no_longer_told_to_add_ext4():
    # A RHEL-family / remote base image roots on XFS (ADR-0183). Before #1626 such a kernel was
    # told to build in EXT4_FS, which its guest never mounts.
    cfg = KernelConfig(frozenset({"XFS_FS", "VIRTIO_BLK"}))
    assert _rootfs_call(cfg) is None


def test_rootfs_missing_one_symbol_warns_and_names_it():
    # VIRTIO_BLK is its own clause (the root *device*, not the filesystem), so a config carrying a
    # root filesystem but no virtio-blk driver still warns.
    cfg = KernelConfig(frozenset({"EXT4_FS"}))
    warning = _rootfs_call(cfg)
    assert warning is not None
    assert warning["reason"] == MISSING_BOOT_CONFIG_REASON
    assert warning["missing"] == ["VIRTIO_BLK"]


def test_rootfs_no_supported_filesystem_names_both_alternatives():
    # BTRFS is a real filesystem kdive never boots from: neither OR-group member is enabled, so
    # the advisory fires and offers both alternatives rather than only ext4.
    cfg = KernelConfig(frozenset({"BTRFS_FS"}))
    warning = _rootfs_call(cfg)
    assert warning is not None
    assert warning["missing"] == ["EXT4_FS", "VIRTIO_BLK", "XFS_FS"]
    assert "mount" in warning["remediation"]


def test_missing_effective_config_nudge_lookup_error_fails_open_with_traceback(
    caplog: pytest.LogCaptureFixture,
):
    async def _boom(conn: Any, run_id: Any) -> None:
        raise RuntimeError("lookup failed")

    with (
        patch("kdive.kernel_config.gate.effective_config_key", _boom),
        caplog.at_level(logging.WARNING, logger="kdive.kernel_config.gate"),
    ):
        got = asyncio.run(missing_effective_config_nudge(_CONN, _RUN_ID))

    assert got is None
    assert len(caplog.records) == 1
    assert str(_RUN_ID) in caplog.records[0].getMessage()
    assert caplog.records[0].exc_info is not None


def test_missing_effective_config_nudge_cancellation_propagates_without_fail_open_warning(
    caplog: pytest.LogCaptureFixture,
):
    async def _cancel(conn: Any, run_id: Any) -> None:
        raise asyncio.CancelledError

    with (
        patch("kdive.kernel_config.gate.effective_config_key", _cancel),
        caplog.at_level(logging.WARNING, logger="kdive.kernel_config.gate"),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(missing_effective_config_nudge(_CONN, _RUN_ID))

    assert not caplog.records


# The legacy-kexec_load config as olddefconfig would leave it: the derived KEXEC_CORE and
# VMCORE_INFO the kernel selects for itself are absent, and since #1854 the gate does not ask
# for them.
_KEXEC_LOAD_ONLY = frozenset(
    {
        "KEXEC",
        "CRASH_DUMP",
        "PROC_VMCORE",
        "FW_CFG_SYSFS",
        "RELOCATABLE",
    }
)


def _crash_call(config: KernelConfig | None) -> dict[str, Any] | None:
    async def _run() -> dict[str, Any] | None:
        with _patched_load(config):
            return await crash_capture_refusal(_CONN, _RUN_ID)

    return asyncio.run(_run())


def test_crash_capture_still_admits_a_legacy_kexec_load_only_kernel():
    # #1626 judgment call: RHEL kdump uses kexec_file_load, so KEXEC alone cannot capture *there*.
    # Tightening the {KEXEC, KEXEC_FILE} OR-group into a hard KEXEC_FILE requirement would refuse
    # installs that capture fine on a non-RHEL guest, and kdive has no guest-family axis to
    # discriminate. The gate deliberately stays an OR; the RHEL gap is advisory instead.
    assert _crash_call(KernelConfig(_KEXEC_LOAD_ONLY)) is None


def test_crash_capture_refusal_remediation_points_at_the_rhel_guest_feature():
    # When the gate does refuse, the remediation must not imply the gated set is sufficient on a
    # RHEL guest — that is exactly the trap #1626 reports.
    refusal = _crash_call(KernelConfig(_KEXEC_LOAD_ONLY - {"FW_CFG_SYSFS"}))
    assert refusal is not None
    assert refusal["reason"] == CRASH_CONFIG_REASON
    assert refusal["missing"] == ["FW_CFG_SYSFS"]
    assert "crash_capture_rhel_guest" in cast(str, refusal["remediation"])
