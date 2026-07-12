"""Unit tests for `debuginfo_warning` (ADR-0322): warn, never refuse, and fail open."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from psycopg import AsyncConnection

from kdive.kernel_config.gate import (
    DEBUGINFO_UNLOADABLE_REASON,
    MISSING_BOOT_CONFIG_REASON,
    MISSING_DEBUGINFO_REASON,
    debuginfo_unloadable_warning,
    debuginfo_warning,
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


def test_rootfs_missing_one_symbol_warns_and_names_it():
    # A real, non-degenerate config missing only VIRTIO_BLK still warns (each advertise clause
    # is required; it is not an OR-group across EXT4_FS/VIRTIO_BLK).
    cfg = KernelConfig(frozenset({"EXT4_FS"}))
    warning = _rootfs_call(cfg)
    assert warning is not None
    assert warning["reason"] == MISSING_BOOT_CONFIG_REASON
    assert warning["missing"] == ["VIRTIO_BLK"]


def test_rootfs_missing_both_symbols_names_both():
    cfg = KernelConfig(frozenset({"XFS_FS"}))  # non-degenerate, but neither boot symbol
    warning = _rootfs_call(cfg)
    assert warning is not None
    assert warning["missing"] == ["EXT4_FS", "VIRTIO_BLK"]
    assert "mount" in warning["remediation"]
