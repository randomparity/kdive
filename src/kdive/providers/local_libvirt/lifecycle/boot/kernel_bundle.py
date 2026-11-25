"""Kernel bundle extraction helpers for local-libvirt installs."""

from __future__ import annotations

import contextlib
import hashlib
import posixpath
import tarfile
import tempfile
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, BinaryIO, Literal, cast

from kdive.build_artifacts.limits import MAX_LEGACY_INSTALL_MODULE_BYTES
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    MAX_ARCHIVE_BYTES,
    MAX_ENTRIES,
    MAX_REGULAR_BYTES,
)
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
MAX_KERNEL_TAR_UNCOMPRESSED_BYTES = MAX_LEGACY_INSTALL_MODULE_BYTES


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


def extract_kernel_bundle(source: IO[bytes], kernel_dest: Path, modules_dest: Path | None) -> bool:
    """Extract ``boot/vmlinuz`` and, optionally, repack ``lib/modules/`` in one streaming pass.

    The unified ``kernel`` artifact is a gzip tar of ``boot/vmlinuz`` + ``lib/modules/<ver>/``
    (ADR-0234). libvirt's direct-kernel ``<kernel>`` element needs a raw kernel-image path — a
    bzImage on x86_64, an ELF ``vmlinux`` on ppc64le (powerpc has no bzImage; ADR-0343/0344) — so
    whatever bytes ``boot/vmlinuz`` carries are extracted host-side via temp-then-rename. The
    extraction is arch-opaque by design: the arch was already validated at upload (ADR-0343), so
    this reads no magic and copies the member verbatim regardless of arch.

    ``source`` is a forward-only binary reader (the S3 body reader from
    ``ObjectStore.get_artifact_stream``, ADR-0400) opened in ``tarfile`` stream mode (``r|gz``): the
    combined tar is neither fully materialized as ``bytes`` nor written to disk before extraction.
    When ``modules_dest`` is given (a kdump or debuginfo install that needs the module tree in the
    guest), the same forward walk repacks the ``lib/modules/`` subtree into ``modules_dest`` and
    returns whether a subtree was found; when it is ``None`` only the boot member is read (the walk
    stops there) and the return is ``False``. The single pass decompresses the boot member once and
    reuses it for both the extract and the skip-into-modules, so a modules-needed run saves one
    boot-member decompression (meaningful when that member is a large ppc64le ELF), and a boot-only
    run stops at the boot member so the caller can close the reader and abort the rest of the
    download (ADR-0399/0400, #1350/#1351). The boot member is written the instant it is read so its
    bytes are not held in RAM across the module repack.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` if ``boot/vmlinuz`` is absent, the stream is
            unreadable/undecompressable, or repacking the modules tar fails (e.g. ``ENOSPC`` on a
            tmpfs scratch — the details name the scratch modules path so an operator is pointed at
            ``KDIVE_INSTALL_SCRATCH`` sizing, not the staging kernel); ``CONFIGURATION_ERROR`` for
            a member-count or uncompressed-size bomb (the ``.part`` modules tar is cleaned before
            the error escapes). A mid-stream store fault the reader has already mapped to a
            ``CategorizedError`` propagates through unwrapped (ADR-0400).
    """
    modules_tmp = modules_dest.with_name(modules_dest.name + ".part") if modules_dest else None
    try:
        return _scan_combined_tar(source, kernel_dest, modules_dest, modules_tmp)
    except (OSError, tarfile.TarError) as exc:
        # The fault is either reading/decompressing the combined tar or writing the repacked
        # modules tar (a full tmpfs scratch surfaces as ENOSPC here). Name both destinations and
        # keep the message neutral so a full-scratch operator is not misdirected at the staging
        # kernel path; modules_dest is present only on a modules-needed run. (A member-count/
        # oversize/missing-boot rejection is already a CategorizedError and propagates unwrapped.)
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
    finally:
        # Clean the partial ``.part`` on every failure exit. On success ``_scan_combined_tar`` has
        # already renamed or unlinked it, so this is a no-op (``missing_ok``); a single site instead
        # of one per except arm.
        if modules_tmp is not None:
            with contextlib.suppress(OSError):
                modules_tmp.unlink(missing_ok=True)


def _scan_combined_tar(
    source: IO[bytes], kernel_dest: Path, modules_dest: Path | None, modules_tmp: Path | None
) -> bool:
    """Single forward streaming pass: extract the boot member, repack modules into the temp tar."""
    boot_written = False
    found = False
    total = 0
    with contextlib.ExitStack() as stack:
        archive = stack.enter_context(tarfile.open(fileobj=source, mode="r|gz"))
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
                    # the rest of the tar. In "r|gz" stream mode advancing past a member reads (and
                    # discards) its bytes from the S3 stream, so iterating to the end would pull the
                    # whole (DWARF-bloated) module tree off the network just to read boot/vmlinuz.
                    # Stopping lets the caller close the reader and abort the rest of the download.
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
            details={"member": _KERNEL_BUNDLE_BOOT_MEMBER},
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


@dataclass(frozen=True, slots=True)
class _ConvertedMember:
    name: str
    mode: int
    uid: int
    gid: int
    kind: Literal["directory", "regular", "symlink"]
    size: int
    target: str
    offset: int


def convert_kernel_bundle_modules(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    release: str,
    temporary_directory: str | None = None,
) -> tuple[str, int]:
    """Convert one raw bundle module tree into Task 2's canonical archive."""
    prefix = f"lib/modules/{release}/"
    entries: list[_ConvertedMember] = []
    seen: set[str] = set()
    regular_bytes = 0
    with tempfile.TemporaryFile(dir=temporary_directory) as content:
        with tarfile.open(fileobj=source, mode="r|gz") as archive:
            for member in archive:
                if member.name == "boot/vmlinuz" or member.name in {
                    "lib",
                    "lib/modules",
                    f"lib/modules/{release}",
                }:
                    continue
                if not member.name.startswith(prefix):
                    raise ValueError("module bundle contains a cross-release or unknown entry")
                name = _canonical_bundle_path(member.name[len(prefix) :])
                if (
                    member.issym()
                    and member.linkname.startswith("/")
                    and name
                    in {
                        "build",
                        "source",
                    }
                ):
                    continue
                if name in seen:
                    raise ValueError("module bundle contains duplicate entries")
                seen.add(name)
                kind = _bundle_member_kind(member)
                target = (
                    _canonical_bundle_target(name, member.linkname) if kind == "symlink" else ""
                )
                size, offset = 0, content.tell()
                if kind == "regular":
                    regular_bytes += member.size
                    if regular_bytes > MAX_REGULAR_BYTES:
                        raise ValueError("module bundle exceeds the regular-byte bound")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ValueError("module bundle regular entry is unreadable")
                    size = _copy_exact(cast(BinaryIO, extracted), content, member.size)
                entries.append(
                    _ConvertedMember(
                        name,
                        member.mode,
                        member.uid,
                        member.gid,
                        kind,
                        size,
                        target,
                        offset,
                    )
                )
                if len(entries) > MAX_ENTRIES:
                    raise ValueError("module bundle exceeds the entry-count bound")
        with tempfile.TemporaryFile(dir=temporary_directory) as converted:
            with tarfile.open(fileobj=converted, mode="w", format=tarfile.PAX_FORMAT) as output:
                for entry in sorted(entries, key=lambda value: value.name.encode()):
                    info = tarfile.TarInfo(entry.name)
                    info.mode, info.uid, info.gid, info.mtime = (
                        entry.mode,
                        entry.uid,
                        entry.gid,
                        0,
                    )
                    info.uname = info.gname = ""
                    info.pax_headers = {"KDIVE.xattrs-supported": "0"}
                    reader: BinaryIO | None = None
                    if entry.kind == "directory":
                        info.type = tarfile.DIRTYPE
                    elif entry.kind == "symlink":
                        info.type, info.linkname = tarfile.SYMTYPE, entry.target
                    else:
                        info.size = entry.size
                        content.seek(entry.offset)
                        reader = cast(BinaryIO, _BoundedSlice(content, entry.size))
                    output.addfile(info, reader)
            size = converted.tell()
            if size > MAX_ARCHIVE_BYTES:
                raise ValueError("canonical module archive exceeds its byte bound")
            converted.seek(0)
            digest = hashlib.sha256()
            while chunk := converted.read(1024 * 1024):
                digest.update(chunk)
                destination.write(chunk)
    return "sha256:" + digest.hexdigest(), size


class _BoundedSlice:
    def __init__(self, source: BinaryIO, remaining: int) -> None:
        self._source, self._remaining = source, remaining

    def read(self, size: int = -1) -> bytes:
        count = self._remaining if size < 0 else min(size, self._remaining)
        data = self._source.read(count)
        self._remaining -= len(data)
        return data


def _copy_exact(source: BinaryIO, destination: BinaryIO, expected: int) -> int:
    remaining = expected
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            raise ValueError("module bundle entry ended before its declared size")
        destination.write(chunk)
        remaining -= len(chunk)
    return expected


def _canonical_bundle_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or unicodedata.normalize("NFC", value) != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("module bundle path is not canonical relative text")
    return value


def _bundle_member_kind(member: tarfile.TarInfo) -> Literal["directory", "regular", "symlink"]:
    if member.isdir():
        return "directory"
    if member.isfile() and not member.islnk():
        return "regular"
    if member.issym():
        return "symlink"
    raise ValueError("module bundle contains forbidden topology")


def _canonical_bundle_target(name: str, target: str) -> str:
    if unicodedata.normalize("NFC", target) != target:
        raise ValueError("module bundle symlink target is not NFC")
    if target.startswith("/"):
        raise ValueError("module bundle symlink escapes the release tree")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError("module bundle symlink escapes the release tree")
    return target
