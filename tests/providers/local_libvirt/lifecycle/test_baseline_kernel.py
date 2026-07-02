from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.baseline_kernel import (
    baseline_kernel_names,
    select_kernel_and_initrd,
)

_V = "6.19.10-300.fc44.x86_64"


def test_fedora_kernel_pairs_with_initramfs() -> None:
    entries = [f"/boot/vmlinuz-{_V}", f"/boot/initramfs-{_V}.img", "/boot/config-x", "/boot/grub2"]
    assert select_kernel_and_initrd(entries) == (f"vmlinuz-{_V}", f"initramfs-{_V}.img")


def test_debian_kernel_pairs_with_initrd_img() -> None:
    v = "6.1.0-13-amd64"
    entries = [f"/boot/vmlinuz-{v}", f"/boot/initrd.img-{v}"]
    assert select_kernel_and_initrd(entries) == (f"vmlinuz-{v}", f"initrd.img-{v}")


def test_kernel_without_initramfs_returns_none() -> None:
    assert select_kernel_and_initrd([f"/boot/vmlinuz-{_V}"]) == (f"vmlinuz-{_V}", None)


def test_rescue_pair_is_excluded_when_a_real_kernel_exists() -> None:
    entries = [
        "/boot/vmlinuz-0-rescue-abc",
        "/boot/initramfs-0-rescue-abc.img",
        f"/boot/vmlinuz-{_V}",
        f"/boot/initramfs-{_V}.img",
    ]
    assert select_kernel_and_initrd(entries) == (f"vmlinuz-{_V}", f"initramfs-{_V}.img")


def test_only_rescue_kernel_raises() -> None:
    with pytest.raises(CategorizedError) as exc:
        select_kernel_and_initrd(["/boot/vmlinuz-0-rescue-abc", "/boot/initramfs-0-rescue-abc.img"])
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_empty_boot_raises() -> None:
    with pytest.raises(CategorizedError) as exc:
        select_kernel_and_initrd([])
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_multiple_kernels_fails_closed_and_names_candidates() -> None:
    a, b = "vmlinuz-6.19.10-300.fc44.x86_64", "vmlinuz-6.18.0-100.fc44.x86_64"
    with pytest.raises(CategorizedError) as exc:
        select_kernel_and_initrd([f"/boot/{a}", f"/boot/{b}"])
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    candidates = exc.value.details["candidates"]
    assert isinstance(candidates, list)
    assert set(candidates) == {a, b}


def test_accepts_bare_basenames_too() -> None:
    assert select_kernel_and_initrd([f"vmlinuz-{_V}", f"initramfs-{_V}.img"]) == (
        f"vmlinuz-{_V}",
        f"initramfs-{_V}.img",
    )


def test_baseline_kernel_names_filters_rescue_and_non_kernels() -> None:
    entries = [
        f"/boot/vmlinuz-{_V}",
        "/boot/vmlinuz-0-rescue-abc",
        f"/boot/initramfs-{_V}.img",
        "/boot/config-x",
    ]
    assert baseline_kernel_names(entries) == [f"vmlinuz-{_V}"]


def test_baseline_kernel_names_accepts_paths_or_basenames() -> None:
    assert baseline_kernel_names([f"/boot/vmlinuz-{_V}"]) == baseline_kernel_names(
        [f"vmlinuz-{_V}"]
    )


@pytest.mark.parametrize(
    "entries",
    [
        [f"/boot/vmlinuz-{_V}", f"/boot/initramfs-{_V}.img"],
        [f"vmlinuz-{_V}"],
    ],
)
def test_baseline_kernel_names_count_one_iff_selection_succeeds(entries: list[str]) -> None:
    # The recorded count predicts the provision-time selection: exactly one baseline candidate is
    # the only provisionable case.
    assert len(baseline_kernel_names(entries)) == 1
    assert select_kernel_and_initrd(entries)[0].startswith("vmlinuz-")


@pytest.mark.parametrize(
    "entries",
    [
        [],
        ["/boot/vmlinuz-0-rescue-abc"],
        ["/boot/vmlinuz-6.19.10-300.fc44.x86_64", "/boot/vmlinuz-6.18.0-100.fc44.x86_64"],
    ],
)
def test_baseline_kernel_names_count_not_one_iff_selection_fails(entries: list[str]) -> None:
    assert len(baseline_kernel_names(entries)) != 1
    with pytest.raises(CategorizedError):
        select_kernel_and_initrd(entries)
