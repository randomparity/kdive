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
from tests.kernel_config.config_fixtures import all_builtin

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
    cfg = all_builtin({"DEBUG_INFO", "DEBUG_INFO_BTF", "DEBUG_KERNEL"})
    assert _call(config=cfg, has_uploaded_vmlinux=False) is None


def test_config_with_dwarf_but_no_btf_still_warns():
    # In-guest drgn-live reads BTF, not the kernel .config's DWARF (the DWARF vmlinux is not on the
    # guest rootfs). A DWARF-only config with no uploaded vmlinux is still blind, so it must warn.
    cfg = all_builtin({"DEBUG_INFO", "DEBUG_INFO_DWARF5", "DEBUG_KERNEL"})
    warning = _call(config=cfg, has_uploaded_vmlinux=False)
    assert warning is not None
    assert warning["missing"] == ["DEBUG_INFO_BTF"]


def test_config_lacking_btf_warns_and_names_btf():
    cfg = all_builtin({"DEBUG_INFO", "DEBUG_KERNEL"})  # no BTF
    warning = _call(config=cfg, has_uploaded_vmlinux=False)
    assert warning is not None
    assert warning["reason"] == MISSING_DEBUGINFO_REASON
    assert warning["missing"] == ["DEBUG_INFO_BTF"]
    assert "vmlinux" in warning["remediation"]


def test_the_missing_debuginfo_remediation_names_a_dwarf_member_and_not_btf_alone():
    # #1855. The warning keys on BTF, and that stays: it asks whether in-guest drgn can read
    # /sys/kernel/btf, which is a different question from whether the kernel carries DWARF. But
    # "enable CONFIG_DEBUG_INFO_BTF" on its own is advice a DEBUG_INFO=n kernel cannot follow -
    # lib/Kconfig.debug:398 puts BTF inside `if DEBUG_INFO` (:325-455) and it selects nothing, so
    # a fragment setting it alone is discarded by olddefconfig and the rebuild changes nothing.
    # The remediation has to name the DWARF choice member that has to be picked first.
    cfg = all_builtin({"DEBUG_KERNEL"})  # DEBUG_INFO=n: the unfollowable case
    warning = _call(config=cfg, has_uploaded_vmlinux=False)
    assert warning is not None
    remediation = cast(str, warning["remediation"])
    # the prerequisite, by at least one settable choice member the agent can put in a fragment
    assert "CONFIG_DEBUG_INFO_DWARF5" in remediation
    # and the symbol the warning is actually keyed on, so the advice stays a two-step instruction
    # rather than being replaced by the prerequisite
    assert "CONFIG_DEBUG_INFO_BTF" in remediation
    # the reason the order matters, so an agent that already has BTF in its fragment understands
    # why the rebuild dropped it rather than reading the two names as interchangeable
    assert "olddefconfig" in remediation
    # unchanged escape hatch: a host vmlinux resolves symbols without touching the kernel config
    assert "vmlinux" in remediation


def test_the_debuginfo_warning_still_keys_on_btf_alone_not_on_the_dwarf_prerequisite():
    # The coupling #1855 must NOT "fix". A kernel carrying DWARF but no BTF is a complete debuginfo
    # build for an offline vmcore and gdb, and in-guest drgn-live is still blind on it, so this
    # seam must keep firing there - naming DWARF in the remediation may not turn into keying on it.
    cfg = all_builtin({"DEBUG_INFO", "DEBUG_INFO_DWARF5", "DEBUG_KERNEL"})
    warning = _call(config=cfg, has_uploaded_vmlinux=False)
    assert warning is not None
    assert warning["missing"] == ["DEBUG_INFO_BTF"]
    # and the inverse: BTF present with no DWARF member named at all is silent, which is what
    # proves the DWARF symbols are remediation prose here and not a second condition
    btf_only = all_builtin({"DEBUG_INFO", "DEBUG_INFO_BTF"})
    assert _call(config=btf_only, has_uploaded_vmlinux=False) is None


def test_unloadable_warning_is_distinct_reason_naming_btf():
    # The runtime-probe payload (ADR-0329) is a distinct reason from the static gate, but shares the
    # {reason, missing, remediation} shape and keys on the same BTF symbol.
    warning = debuginfo_unloadable_warning()
    assert warning["reason"] == DEBUGINFO_UNLOADABLE_REASON
    assert warning["reason"] != MISSING_DEBUGINFO_REASON
    assert warning["missing"] == ["DEBUG_INFO_BTF"]
    assert "vmlinux" in cast(str, warning["remediation"])


def _rootfs_call(
    config: KernelConfig | None,
    *,
    has_initrd: bool = False,
    guest_builds_initramfs: bool = False,
) -> dict[str, Any] | None:
    async def _run() -> dict[str, Any] | None:
        with _patched_load(config):
            return await rootfs_mount_warning(
                _CONN,
                _RUN_ID,
                has_initrd=has_initrd,
                guest_builds_initramfs=guest_builds_initramfs,
            )

    return asyncio.run(_run())


def test_rootfs_absent_config_fails_open_to_no_warning():
    assert _rootfs_call(None) is None


def test_rootfs_full_boot_set_produces_no_warning():
    cfg = all_builtin({"EXT4_FS", "VIRTIO_BLK"})
    assert _rootfs_call(cfg) is None


def test_rootfs_ext4_local_libvirt_kernel_stays_silent_after_adding_xfs():
    # #1626 regression guard: XFS_FS joined rootfs_mount as an OR-group member, not as a second
    # required clause. Appending it with _plain would have made every ext4-root local-libvirt
    # kernel — the overwhelmingly common case — start emitting a spurious missing_boot_config.
    cfg = all_builtin({"EXT4_FS", "VIRTIO_BLK", "KEXEC", "MAGIC_SYSRQ"})
    assert _rootfs_call(cfg) is None


def test_rootfs_xfs_only_kernel_is_no_longer_told_to_add_ext4():
    # A RHEL-family / remote base image roots on XFS (ADR-0183). Before #1626 such a kernel was
    # told to build in EXT4_FS, which its guest never mounts.
    cfg = all_builtin({"XFS_FS", "VIRTIO_BLK"})
    assert _rootfs_call(cfg) is None


def test_rootfs_missing_one_symbol_warns_and_names_it():
    # VIRTIO_BLK is its own clause (the root *device*, not the filesystem), so a config carrying a
    # root filesystem but no virtio-blk driver still warns.
    cfg = all_builtin({"EXT4_FS"})
    warning = _rootfs_call(cfg)
    assert warning is not None
    assert warning["reason"] == MISSING_BOOT_CONFIG_REASON
    assert warning["missing"] == ["VIRTIO_BLK"]


def test_rootfs_no_supported_filesystem_names_both_alternatives():
    # BTRFS is a real filesystem kdive never boots from: neither OR-group member is enabled, so
    # the advisory fires and offers both alternatives rather than only ext4.
    cfg = all_builtin({"BTRFS_FS"})
    warning = _rootfs_call(cfg)
    assert warning is not None
    assert warning["missing"] == ["EXT4_FS", "VIRTIO_BLK", "XFS_FS"]
    assert "mount" in warning["remediation"]


def test_a_modular_boot_kernel_with_no_initrd_now_warns_and_says_the_symbols_are_modular():
    # #1860's failure: CONFIG_VIRTIO_BLK=m parsed as enabled, the advisory stayed silent, install
    # succeeded and the guest panicked on an unmountable root. `missing` names VIRTIO_BLK because
    # its clause is unmet, and `built_in_required` is what stops that reading as a kdive bug to
    # the agent holding a config that visibly contains the symbol.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}), frozenset({"EXT4_FS"}))
    warning = _rootfs_call(cfg)
    assert warning is not None
    assert warning["reason"] == MISSING_BOOT_CONFIG_REASON
    assert warning["missing"] == ["VIRTIO_BLK"]
    assert warning["built_in_required"] == ["VIRTIO_BLK"]


def test_an_uploaded_initrd_relieves_the_same_modular_kernel():
    # The carve-out, evaluated at the seam: an initrd artifact is where the module comes from, so
    # the identical config draws no warning once the build uploaded one.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}), frozenset({"EXT4_FS"}))
    assert _rootfs_call(cfg, has_initrd=True) is None


def test_the_seam_defaults_to_the_strict_reading_when_neither_relief_fact_is_supplied():
    # ADR-0330's direction for this warning: over-warn rather than fall silent, which ADR-0545
    # extends to both reliefs. The keywords are OMITTED at the real call site rather than passed
    # as False through `_rootfs_call`: the helper has defaults of its own, so routing through it
    # would assert the helper's default and let a flipped default on the seam survive.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}), frozenset({"EXT4_FS"}))

    async def _omitting_both() -> dict[str, Any] | None:
        with _patched_load(cfg):
            return await rootfs_mount_warning(_CONN, _RUN_ID)

    async def _omitting_only_the_boot_model() -> dict[str, Any] | None:
        with _patched_load(cfg):
            return await rootfs_mount_warning(_CONN, _RUN_ID, has_initrd=False)

    async def _omitting_only_the_initrd() -> dict[str, Any] | None:
        with _patched_load(cfg):
            return await rootfs_mount_warning(_CONN, _RUN_ID, guest_builds_initramfs=False)

    assert asyncio.run(_omitting_both()) is not None
    # one arm per keyword, so a default flipped on either one alone is reported here
    assert asyncio.run(_omitting_only_the_boot_model()) is not None
    assert asyncio.run(_omitting_only_the_initrd()) is not None


def test_a_guest_that_builds_its_own_initramfs_relieves_the_modular_kernel_without_an_upload():
    # #1881 / ADR-0545: the disk-image lane never uploads an initrd - remote-libvirt rejects the
    # component outright - and its in-guest installer runs dracut, so the module is loadable before
    # root is mounted. The identical config that warns on the direct-kernel lane is silent here.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}), frozenset({"EXT4_FS"}))
    assert _rootfs_call(cfg, guest_builds_initramfs=True) is None


def test_the_two_reliefs_are_independent_and_either_one_alone_silences_the_advisory():
    # Neither fact is a proxy for the other: a direct-kernel Run that uploaded an initrd and a
    # disk-image Run that did not are both relieved, and the strict verdict needs both to be
    # false. Without this an implementation that ANDed them would still pass the two tests above.
    cfg = KernelConfig(frozenset({"EXT4_FS", "VIRTIO_BLK"}), frozenset({"EXT4_FS"}))
    assert _rootfs_call(cfg, has_initrd=True, guest_builds_initramfs=False) is None
    assert _rootfs_call(cfg, has_initrd=False, guest_builds_initramfs=True) is None
    assert _rootfs_call(cfg, has_initrd=True, guest_builds_initramfs=True) is None
    assert _rootfs_call(cfg, has_initrd=False, guest_builds_initramfs=False) is not None


def test_the_boot_model_relief_does_not_silence_a_symbol_the_config_lacks_outright():
    # The relief is about *when* a module can load, not about whether the kernel has the driver.
    # A disk-image guest whose kernel carries no virtio-blk at all still cannot mount its root, so
    # dracut has nothing to package - the advisory must survive the widened carve-out.
    warning = _rootfs_call(all_builtin({"EXT4_FS"}), guest_builds_initramfs=True)
    assert warning is not None
    assert warning["reason"] == MISSING_BOOT_CONFIG_REASON
    assert warning["missing"] == ["VIRTIO_BLK"]


def test_built_in_required_is_absent_when_every_missing_symbol_is_absent_outright():
    # The key is optional so a client keying on the ADR-0330 {reason, missing, remediation} shape
    # is unaffected. A kernel with no virtio-blk at all gets no key, not an empty list.
    warning = _rootfs_call(all_builtin({"EXT4_FS"}))
    assert warning is not None
    assert warning["missing"] == ["VIRTIO_BLK"]
    assert "built_in_required" not in warning


def test_a_built_in_boot_kernel_is_silent_whether_or_not_an_initrd_was_uploaded():
    cfg = all_builtin({"EXT4_FS", "VIRTIO_BLK"})
    assert _rootfs_call(cfg) is None
    assert _rootfs_call(cfg, has_initrd=True) is None


def test_the_crash_refusal_payload_gains_no_key_from_a_modular_kexec_kernel():
    # crash_capture carries no built-in requirement - its seam supplies no initrd fact, so it may
    # not (invariant I2) - which means a modular KEXEC still satisfies its clause and never
    # reaches `missing`. The key is therefore absent here by construction, not by omission.
    modular = KernelConfig(_KEXEC_LOAD_ONLY, _KEXEC_LOAD_ONLY - {"KEXEC"})
    assert _crash_call(modular) is None
    refusal = _crash_call(
        KernelConfig(_KEXEC_LOAD_ONLY - {"FW_CFG_SYSFS"}, _KEXEC_LOAD_ONLY - {"FW_CFG_SYSFS"})
    )
    assert refusal is not None
    assert refusal["missing"] == ["FW_CFG_SYSFS"]
    assert "built_in_required" not in refusal


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


_KEXEC_LOAD_ONLY = frozenset(
    {
        "KEXEC",
        "KEXEC_CORE",
        "CRASH_DUMP",
        "PROC_VMCORE",
        "VMCORE_INFO",
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
    assert _crash_call(all_builtin(_KEXEC_LOAD_ONLY)) is None


def test_crash_capture_refusal_remediation_points_at_the_rhel_guest_feature():
    # When the gate does refuse, the remediation must not imply the gated set is sufficient on a
    # RHEL guest — that is exactly the trap #1626 reports.
    refusal = _crash_call(all_builtin(_KEXEC_LOAD_ONLY - {"FW_CFG_SYSFS"}))
    assert refusal is not None
    assert refusal["reason"] == CRASH_CONFIG_REASON
    assert refusal["missing"] == ["FW_CFG_SYSFS"]
    assert "crash_capture_rhel_guest" in cast(str, refusal["remediation"])
