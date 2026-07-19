"""Direct unit tests for the provider-neutral ``/boot`` baseline classifier."""

from __future__ import annotations

from kdive.images.rootfs.baseline import VMLINUZ_PREFIX, baseline_kernel_names


def test_keeps_only_non_rescue_vmlinuz_basenames() -> None:
    entries = [
        "/boot/vmlinuz-6.1.0",
        "vmlinuz-6.1.0-rescue",
        "/boot/initramfs-6.1.0.img",
        "config-6.1.0",
        "vmlinuz-5.14.0",
    ]
    assert baseline_kernel_names(entries) == ["vmlinuz-6.1.0", "vmlinuz-5.14.0"]


def test_reduces_full_paths_to_basenames() -> None:
    assert baseline_kernel_names(["/some/deep/path/vmlinuz-6.1.0"]) == ["vmlinuz-6.1.0"]


def test_rescue_images_are_excluded_regardless_of_position() -> None:
    assert baseline_kernel_names(["vmlinuz-6.1.0-rescue-abc"]) == []


def test_non_vmlinuz_entries_are_excluded() -> None:
    assert baseline_kernel_names(["initramfs.img", "config-6.1", "System.map"]) == []


def test_empty_listing_yields_no_candidates() -> None:
    assert baseline_kernel_names([]) == []


def test_prefix_constant_matches_classifier() -> None:
    assert VMLINUZ_PREFIX == "vmlinuz-"
    assert baseline_kernel_names([f"{VMLINUZ_PREFIX}9.9.9"]) == [f"{VMLINUZ_PREFIX}9.9.9"]
