from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.baseline_kernel import select_kernel_and_initrd

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
