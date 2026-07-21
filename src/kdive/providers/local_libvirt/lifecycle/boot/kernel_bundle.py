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


def extract_kernel_bundle(combined_tar: Path, kernel_dest: Path, modules_dest: Path | None) -> bool:
    """Extract ``boot/vmlinuz`` and, optionally, repack ``lib/modules/`` in one decompression pass.

    The unified ``kernel`` artifact is a gzip tar of ``boot/vmlinuz`` + ``lib/modules/<ver>/``
    (ADR-0234). libvirt's direct-kernel ``<kernel>`` element needs a raw kernel-image path — a
    bzImage on x86_64, an ELF ``vmlinux`` on ppc64le (powerpc has no bzImage; ADR-0343/0344) — so
    whatever bytes ``boot/vmlinuz`` carries are extracted host-side via temp-then-rename. The
    extraction is arch-opaque by design: the arch was already validated at upload (ADR-0343), so
    this reads no magic and copies the member verbatim regardless of arch.

    When ``modules_dest`` is given (a kdump or debuginfo install that needs the module tree in the
    guest), the same forward walk repacks the ``lib/modules/`` subtree into ``modules_dest`` and
    returns whether a subtree was found; when it is ``None`` only the boot member is read (the walk
    stops there) and the return is ``False``. The former two helpers opened the tar twice: the
    repack pass had to decompress the boot member again just to skip past it into ``lib/modules/``.
    This single pass decompresses the boot member once and reuses it for both the extract and the
    skip-into-modules, so a modules-needed run saves one boot-member decompression (meaningful when
    that member is a large ppc64le ELF), and a boot-only run early-exits at the boot member exactly
    as the old next()-based extract did (ADR-0399, #1350). The boot member is written the instant it
    is read so its bytes are not held in RAM across the module repack.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` if ``boot/vmlinuz`` is absent, the tar is
            unreadable, or repacking the modules tar fails (e.g. ``ENOSPC`` on a tmpfs scratch —
            the details name the scratch modules path so an operator is pointed at
            ``KDIVE_INSTALL_SCRATCH`` sizing, not the staging kernel); ``CONFIGURATION_ERROR`` for
            a member-count or uncompressed-size bomb (the ``.part`` modules tar is cleaned before
            the error escapes).
    """
    modules_tmp = modules_dest.with_name(modules_dest.name + ".part") if modules_dest else None
    try:
        return _scan_combined_tar(combined_tar, kernel_dest, modules_dest, modules_tmp)
    except CategorizedError:
        # A missing-member or oversize/count rejection: clean the partial temp tar, keep category.
        if modules_tmp is not None:
            with contextlib.suppress(OSError):
                modules_tmp.unlink()
        raise
    except (OSError, tarfile.TarError) as exc:
        if modules_tmp is not None:
            with contextlib.suppress(OSError):
                modules_tmp.unlink()
        # The fault is either reading/decompressing the combined tar or writing the repacked
        # modules tar (a full tmpfs scratch surfaces as ENOSPC here). Name both destinations and
        # keep the message neutral so a full-scratch operator is not misdirected at the staging
        # kernel path; modules_dest is present only on a modules-needed run.
        details: dict[str, object] = {
            "op": "extract_kernel_bundle",
            "kernel_dest": str(kernel_dest),
        }
        if modules_dest is not None:
            details["modules_dest"] = str(modules_dest)
        raise CategorizedError(
            "failed to read the combined kernel tar or write the repacked modules tar",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details=details,
        ) from exc


def _scan_combined_tar(
    combined_tar: Path, kernel_dest: Path, modules_dest: Path | None, modules_tmp: Path | None
) -> bool:
    """Single forward pass: extract the boot member and repack modules into ``modules_tmp``."""
    boot_written = False
    found = False
    total = 0
    with contextlib.ExitStack() as stack:
        archive = stack.enter_context(tarfile.open(combined_tar, "r:gz"))
        out = (
            stack.enter_context(tarfile.open(modules_tmp, "w:gz"))
            if modules_tmp is not None
            else None
        )
        for member in capped_tar_members(archive):
            normalized = _tar_member_path(member.name)
            if not boot_written and normalized == _KERNEL_BUNDLE_BOOT_MEMBER:
                boot_written = _extract_boot_member(archive, member, kernel_dest)
                if boot_written and out is None:
                    # Boot-only run (no modules repack): stop at the boot member instead of walking
                    # the rest of the tar. In "r:gz" mode advancing past a member decompresses it,
                    # so iterating to the end would decompress the whole (DWARF-bloated) module tree
                    # just to read boot/vmlinuz. This is the early exit the old next()-extract had.
                    break
            elif out is not None and normalized.startswith(_MODULES_MEMBER_PREFIX):
                if ".." in normalized.split("/"):
                    continue
                total = _repack_module_member(archive, out, member, normalized, total, modules_dest)
                found = True
    if not boot_written:
        raise CategorizedError(
            "combined kernel tar has no boot/vmlinuz member",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"member": _KERNEL_BUNDLE_BOOT_MEMBER, "tar": str(combined_tar)},
        )
    if modules_dest is not None and modules_tmp is not None:
        if found:
            modules_tmp.replace(modules_dest)
        else:
            modules_tmp.unlink(missing_ok=True)
    return found


def _extract_boot_member(archive: tarfile.TarFile, member: tarfile.TarInfo, dest: Path) -> bool:
    """Write the boot member to ``dest`` at once (not held in RAM); return whether it was a file."""
    # Refuse a decompression-bomb boot member before read() allocates member.size bytes.
    reject_oversize_member(member.size, dest=str(dest))
    extracted = archive.extractfile(member)
    if extracted is None:  # a directory/link named boot/vmlinuz — treat as no usable boot member
        return False
    write_staged_bytes(dest, extracted.read())
    return True


def _repack_module_member(
    archive: tarfile.TarFile,
    out: tarfile.TarFile,
    member: tarfile.TarInfo,
    normalized: str,
    total: int,
    modules_dest: Path | None,
) -> int:
    """Add one ``lib/modules/`` member to the output tar, enforcing the cumulative-size bound."""
    total += member.size if member.isfile() else 0
    reject_oversize_member(total, dest=str(modules_dest))
    safe_member = member.replace(name=normalized)
    out.addfile(safe_member, archive.extractfile(member) if member.isfile() else None)
    return total
