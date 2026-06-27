"""Kernel bundle extraction helpers for local-libvirt installs."""

from __future__ import annotations

import contextlib
import tarfile
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

_KERNEL_BUNDLE_BOOT_MEMBER = "boot/vmlinuz"
_MODULES_MEMBER_PREFIX = "lib/modules/"


def _write_staged_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` through a sibling temp file, then atomically replace ``dest``."""
    tmp = dest.with_name(dest.name + ".part")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
        tmp.replace(dest)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise CategorizedError(
            "failed to write the staged object to the per-Run path",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "stage", "dest": str(dest)},
        ) from exc


def _tar_member_path(name: str) -> str:
    """Normalize a tar member name for prefix matching."""
    if name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def extract_boot_vmlinuz(combined_tar: Path, dest: Path) -> None:
    """Extract ``boot/vmlinuz`` from the combined kernel tar to ``dest``.

    The unified ``kernel`` artifact is a gzip tar of ``boot/vmlinuz`` + ``lib/modules/<ver>/``
    (ADR-0234). libvirt's direct-kernel ``<kernel>`` element needs a raw bzImage path, so the
    bzImage is extracted host-side via temp-then-rename.
    """
    try:
        with tarfile.open(combined_tar, "r:gz") as archive:
            member = next(
                (
                    item
                    for item in archive.getmembers()
                    if _tar_member_path(item.name) == _KERNEL_BUNDLE_BOOT_MEMBER
                ),
                None,
            )
            extracted = archive.extractfile(member) if member is not None else None
            data = extracted.read() if extracted is not None else None
    except (OSError, tarfile.TarError) as exc:
        raise CategorizedError(
            "failed to read the combined kernel tar to extract boot/vmlinuz",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "extract", "member": _KERNEL_BUNDLE_BOOT_MEMBER, "dest": str(dest)},
        ) from exc
    if data is None:
        raise CategorizedError(
            "combined kernel tar has no boot/vmlinuz member",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"member": _KERNEL_BUNDLE_BOOT_MEMBER, "tar": str(combined_tar)},
        )
    _write_staged_bytes(dest, data)


def repack_modules_subtree(combined_tar: Path, dest: Path) -> bool:
    """Repack the combined kernel tar's ``lib/modules/`` subtree into a modules-only gzip tar."""
    tmp = dest.with_name(dest.name + ".part")
    found = False
    try:
        with tarfile.open(combined_tar, "r:gz") as src, tarfile.open(tmp, "w:gz") as out:
            for member in src.getmembers():
                normalized = _tar_member_path(member.name)
                if ".." in normalized.split("/"):
                    continue
                if normalized.startswith(_MODULES_MEMBER_PREFIX):
                    out.addfile(member, src.extractfile(member) if member.isfile() else None)
                    found = True
        if found:
            tmp.replace(dest)
        else:
            tmp.unlink(missing_ok=True)
    except (OSError, tarfile.TarError) as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise CategorizedError(
            "failed to repack the lib/modules subtree from the combined kernel tar",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "repack", "dest": str(dest)},
        ) from exc
    return found
