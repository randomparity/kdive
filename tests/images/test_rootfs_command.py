"""Test the debug image rootfs packages (Task 6 — kdump-utils)."""

from __future__ import annotations

from kdive.images.rootfs_command import DEFAULT_DEBUG_FS_PACKAGES


def test_debug_image_ships_kdump_service_package() -> None:
    """The debug image must include kdump-utils, kexec-tools, and makedumpfile."""
    assert "kdump-utils" in DEFAULT_DEBUG_FS_PACKAGES
    assert "kexec-tools" in DEFAULT_DEBUG_FS_PACKAGES
    assert "makedumpfile" in DEFAULT_DEBUG_FS_PACKAGES


def test_debug_image_ships_keyutils_for_kdumpctl() -> None:
    """The debug image must ship keyutils (`keyctl`), which `kdumpctl` invokes (ADR-0212, #688)."""
    assert "keyutils" in DEFAULT_DEBUG_FS_PACKAGES
