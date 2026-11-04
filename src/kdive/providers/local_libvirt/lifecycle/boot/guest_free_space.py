"""Refuse to write the DWARF vmlinux into a guest that has no room for it (#3125)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from kdive.domain.errors import CategorizedError, ErrorCategory

# Measured on 6.12.60 DWARF4 unstripped builds (#3130 live proofs): x86_64 510,404,448 B and
# ppc64le 425,486,808 B. One x86_64-sized vmlinux of headroom leaves room for the kdump
# initramfs rebuild, crash dumps and logs after the file lands.
VMLINUX_HEADROOM_BYTES: Final = 512 * 1024 * 1024

# Real filesystem block sizes stop at 64 KiB; 1 MiB leaves slack and still rejects absurd values.
_MAX_FRAGMENT_BYTES: Final = 1024 * 1024
_MAX_BYTES: Final = 2**63 - 1


class GuestFreeSpaceRefusal(CategorizedError):
    """``install_failure`` whose ``details`` hold the sizes proving the shortfall (ADR-0814)."""


def require_free_space_for_vmlinux(
    statvfs: Callable[[str], dict[str, int]],
    directory: str,
    *,
    size: int,
    target: str,
    reclaimed: int = 0,
) -> None:
    """Raise ``install_failure`` unless ``directory``'s filesystem fits ``size`` plus headroom.

    ``statvfs`` is the libguestfs handle's call, so the figure comes from the guest filesystem
    the file is about to be written to. The values come from guest-controlled metadata:
    anything negative, non-integer, inconsistent or past 63 bits is refused rather than
    trusted. Free space is ``bavail`` (what a non-reserved write may use) plus ``reclaimed``,
    the size of a file the upload overwrites in place. ``target`` names the overlay or System
    in the error details.
    """
    free = _free_bytes(statvfs(directory), target, directory) + reclaimed
    needed = size + VMLINUX_HEADROOM_BYTES
    if free >= needed:
        return
    raise GuestFreeSpaceRefusal(
        f"the guest filesystem at {directory} has {free} bytes free but the {size}-byte "
        f"vmlinux needs {needed} bytes including {VMLINUX_HEADROOM_BYTES} bytes of headroom; "
        f"enlarge the System root disk by at least {needed - free} bytes or build a smaller "
        "debuginfo",
        category=ErrorCategory.INSTALL_FAILURE,
        details={
            "target": target,
            "directory": directory,
            "vmlinux_bytes": size,
            "free_bytes": free,
            "headroom_bytes": VMLINUX_HEADROOM_BYTES,
        },
    )


def _free_bytes(stats: dict[str, int], target: str, directory: str) -> int:
    try:
        bavail, blocks, fragment = stats["bavail"], stats["blocks"], stats["frsize"]
    except KeyError as exc:
        raise _implausible(target, directory, f"is missing {exc.args[0]}") from exc
    values = (bavail, blocks, fragment)
    if not all(type(value) is int and 0 <= value <= _MAX_BYTES for value in values):
        raise _implausible(target, directory, "has a negative, oversized or non-integer field")
    if not 0 < fragment <= _MAX_FRAGMENT_BYTES or bavail > blocks or blocks * fragment > _MAX_BYTES:
        raise _implausible(target, directory, "is internally inconsistent")
    return bavail * fragment


def _implausible(target: str, directory: str, reason: str) -> CategorizedError:
    return CategorizedError(
        f"the guest filesystem statistics for {directory} cannot be trusted: the report "
        f"{reason}; repair or rebuild the System root filesystem",
        category=ErrorCategory.INSTALL_FAILURE,
        details={"target": target, "directory": directory},
    )
