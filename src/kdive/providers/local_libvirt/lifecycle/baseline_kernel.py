"""Baseline-kernel extraction from a local-libvirt rootfs base (ADR-0272).

A `direct-kernel` provision boots the rootfs's own kernel: the bootloader-less whole-disk ext4
rootfs (ADR-0030/0052) has a kernel under `/boot` but no in-image bootloader, so the kernel must be
extracted host-side and rendered as a libvirt `<kernel>`. :func:`select_kernel_and_initrd` is the
pure, fail-closed selection; :func:`_real_extract_baseline_kernel` is the `live_vm` libguestfs read.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

_VMLINUZ_PREFIX = "vmlinuz-"


@dataclass(frozen=True, slots=True)
class BaselineKernel:
    """The baseline kernel image and its optional initramfs, as host paths."""

    kernel: Path
    initrd: Path | None


type ExtractBaselineKernel = Callable[[Path, Path], BaselineKernel]
"""Seam: extract the baseline kernel+initramfs from ``base`` into ``dest_dir`` (atomic)."""


def baseline_kernel_names(boot_entries: list[str]) -> list[str]:
    """The non-rescue ``vmlinuz-<ver>`` basenames in a ``/boot`` listing — the baseline candidates.

    Accepts full paths or bare basenames (each is reduced to its basename). Non-``vmlinuz`` entries
    and rescue images are excluded. This is the single classifier both the fail-closed provision
    selection (:func:`select_kernel_and_initrd`) and the build-time ``boot_kernel_count`` capture
    use, so the recorded count predicts the provision-time selection outcome: exactly one candidate
    is the only provisionable case (ADR-0272/0295).
    """
    names = [os.path.basename(entry) for entry in boot_entries]
    return [n for n in names if n.startswith(_VMLINUZ_PREFIX) and "rescue" not in n]


def select_kernel_and_initrd(boot_entries: list[str]) -> tuple[str, str | None]:
    """Pick the System's ``vmlinuz-<ver>`` and matching initramfs from a ``/boot`` listing.

    Fails closed (a silent wrong pick boots a dead guest that still reports ``ready``, #905):
    rescue images are excluded, and zero or more-than-one non-rescue kernel raises rather than
    guessing a version order. Returns basenames; the initramfs is ``None`` for an
    embedded-initramfs kernel.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when there is no non-rescue kernel, or more than
            one (the kdive-ready build emits exactly one).
    """
    names = [os.path.basename(entry) for entry in boot_entries]
    kernels = baseline_kernel_names(boot_entries)
    if not kernels:
        raise CategorizedError(
            "rootfs /boot has no bootable kernel; image cannot direct-kernel boot",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"boot_entries": sorted(names)},
        )
    if len(kernels) > 1:
        raise CategorizedError(
            "rootfs /boot has multiple kernels; cannot select a baseline kernel unambiguously",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"candidates": sorted(kernels)},
        )
    kernel = kernels[0]
    version = kernel[len(_VMLINUZ_PREFIX) :]
    initrd = next(
        (n for n in (f"initramfs-{version}.img", f"initrd.img-{version}") if n in names), None
    )
    return kernel, initrd


def _real_extract_baseline_kernel(  # pragma: no cover - live_vm (libguestfs)
    base: Path, dest_dir: Path
) -> BaselineKernel:
    """Mount ``base`` read-only via libguestfs and stage its baseline kernel+initramfs.

    Downloads into a sibling ``.part`` directory and renames it onto ``dest_dir`` atomically, so a
    crash mid-extraction never leaves a half-populated baseline directory (ADR-0272): the kernel
    and its initramfs are a unit, and a modular kernel cannot boot without its initramfs.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` if the guestfs binding is absent;
            ``INFRASTRUCTURE_FAILURE`` on a libguestfs fault; ``CONFIGURATION_ERROR`` from
            :func:`select_kernel_and_initrd`.
    """
    try:
        import guestfs  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
    except ImportError as exc:
        raise CategorizedError(
            "libguestfs (the guestfs Python binding) is required to extract the baseline kernel",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc
    guest = guestfs.GuestFS(python_return_dict=True)
    tmp = dest_dir.parent / (dest_dir.name + ".part")
    try:
        guest.add_drive_opts(str(base), format="qcow2", readonly=True)
        guest.launch()
        roots = guest.inspect_os()
        if not roots:
            raise CategorizedError(
                "could not inspect the rootfs base to extract the baseline kernel",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"base": str(base)},
            )
        guest.mount_ro(roots[0], "/")
        kernel_name, initrd_name = select_kernel_and_initrd(guest.glob_expand("/boot/*"))
        _reset_dir(tmp)
        guest.download(f"/boot/{kernel_name}", str(tmp / "kernel"))
        if initrd_name is not None:
            guest.download(f"/boot/{initrd_name}", str(tmp / "initrd"))
    except CategorizedError:
        raise
    except Exception as exc:
        raise CategorizedError(
            "libguestfs failed extracting the baseline kernel from the rootfs base",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"base": str(base), "error": type(exc).__name__},
        ) from exc
    finally:
        _shutdown(guest)
    os.rename(tmp, dest_dir)
    return BaselineKernel(
        kernel=dest_dir / "kernel", initrd=(dest_dir / "initrd") if initrd_name else None
    )


def _reset_dir(path: Path) -> None:  # pragma: no cover - live_vm
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)


def _shutdown(guest: object) -> None:  # pragma: no cover - live_vm
    for method in ("shutdown", "close"):
        with contextlib.suppress(Exception):
            getattr(guest, method)()
