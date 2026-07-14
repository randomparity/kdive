"""Kernel bundle extraction helpers for local-libvirt installs."""

from __future__ import annotations

import contextlib
import tarfile
from collections.abc import Iterator
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.boot.staged_write import write_staged_bytes

_KERNEL_BUNDLE_BOOT_MEMBER = "boot/vmlinuz"
_MODULES_MEMBER_PREFIX = "lib/modules/"
# Upper bound on tar members read from a semi-trusted uploaded kernel tar. ``getmembers()`` /
# ``getnames()`` eagerly build one ``TarInfo`` per member, so a header bomb (a tiny gzip encoding
# 10^8 near-identical 512-byte headers) would OOM the worker before any content is read. Every scan
# of the uploaded tar iterates lazily through ``capped_tar_members`` instead, rejecting past this
# bound. A real combined kernel tar (boot/vmlinuz + one module tree) is far under it (#1148 review).
MAX_KERNEL_TAR_MEMBERS = 200_000
# Upper bound on decompressed bytes read from an uploaded kernel tar — per single member (a
# ``boot/vmlinuz`` read into RAM) and cumulative (the repacked/extracted module tree). A member's
# declared size is an attacker-controlled header field and a gzip run of zeros is tiny compressed
# yet can declare tens of GB, so a read is refused past this bound before it allocates. A real
# vmlinuz is well under 2 GiB and a one-version module tree far under it (#1148 review).
MAX_KERNEL_TAR_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024


def reject_oversize_member(size: int, *, dest: str) -> None:
    """Reject a tar member whose decompressed size exceeds the uncompressed bound.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when ``size`` exceeds
            :data:`MAX_KERNEL_TAR_UNCOMPRESSED_BYTES` — a decompression-bomb upload, not a
            transient fault, refused before the bytes are read into memory.
    """
    if size > MAX_KERNEL_TAR_UNCOMPRESSED_BYTES:
        raise CategorizedError(
            "uploaded kernel tar member exceeds the uncompressed-size bound",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"max_uncompressed_bytes": MAX_KERNEL_TAR_UNCOMPRESSED_BYTES, "dest": dest},
        )


def capped_tar_members(archive: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    """Yield tar members lazily, rejecting a member-count bomb before the full header list loads.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` once more than :data:`MAX_KERNEL_TAR_MEMBERS`
            members have been seen — an oversized/hostile upload, not a transient fault.
    """
    for count, member in enumerate(archive, start=1):
        if count > MAX_KERNEL_TAR_MEMBERS:
            raise CategorizedError(
                "uploaded kernel tar exceeds the member-count bound",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"max_members": MAX_KERNEL_TAR_MEMBERS},
            )
        yield member


def _tar_member_path(name: str) -> str:
    """Normalize a tar member name for prefix matching."""
    if name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def extract_boot_vmlinuz(combined_tar: Path, dest: Path) -> None:
    """Extract ``boot/vmlinuz`` from the combined kernel tar to ``dest``.

    The unified ``kernel`` artifact is a gzip tar of ``boot/vmlinuz`` + ``lib/modules/<ver>/``
    (ADR-0234). libvirt's direct-kernel ``<kernel>`` element needs a raw kernel-image path — a
    bzImage on x86_64, an ELF ``vmlinux`` on ppc64le (powerpc has no bzImage; ADR-0343/0344) — so
    whatever bytes the ``boot/vmlinuz`` member carries are extracted host-side via temp-then-rename.
    The extraction is arch-opaque by design: the arch was already validated at upload (ADR-0343),
    so this reads no magic and copies the member verbatim regardless of arch.
    """
    try:
        with tarfile.open(combined_tar, "r:gz") as archive:
            member = next(
                (
                    item
                    for item in capped_tar_members(archive)
                    if _tar_member_path(item.name) == _KERNEL_BUNDLE_BOOT_MEMBER
                ),
                None,
            )
            if member is not None:
                # Refuse a decompression-bomb boot member before read() allocates member.size bytes.
                reject_oversize_member(member.size, dest=str(dest))
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
    write_staged_bytes(dest, data)


def repack_modules_subtree(combined_tar: Path, dest: Path) -> bool:
    """Repack the combined kernel tar's ``lib/modules/`` subtree into a modules-only gzip tar."""
    tmp = dest.with_name(dest.name + ".part")
    found = False
    total = 0
    try:
        with tarfile.open(combined_tar, "r:gz") as src, tarfile.open(tmp, "w:gz") as out:
            for member in capped_tar_members(src):
                normalized = _tar_member_path(member.name)
                if ".." in normalized.split("/"):
                    continue
                if normalized.startswith(_MODULES_MEMBER_PREFIX):
                    total += member.size if member.isfile() else 0
                    reject_oversize_member(total, dest=str(dest))
                    safe_member = member.replace(name=normalized)
                    out.addfile(safe_member, src.extractfile(member) if member.isfile() else None)
                    found = True
        if found:
            tmp.replace(dest)
        else:
            tmp.unlink(missing_ok=True)
    except CategorizedError:
        # An oversize-member rejection: clean up the partial temp tar, keep the category.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    except (OSError, tarfile.TarError) as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise CategorizedError(
            "failed to repack the lib/modules subtree from the combined kernel tar",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "repack", "dest": str(dest)},
        ) from exc
    return found
