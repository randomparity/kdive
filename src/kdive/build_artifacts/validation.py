"""Provider-neutral validation for externally uploaded build artifacts."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import json
import lzma
import posixpath
import re
import struct
import tarfile
import tempfile
import unicodedata
import zlib
from collections.abc import Iterator, Mapping, Sequence
from compression import zstd
from dataclasses import dataclass, field
from typing import IO, Literal, Protocol, cast

from kdive.artifacts.storage import HeadResult
from kdive.artifacts.uploads.chunks import HeadStore
from kdive.artifacts.uploads.uploads import ManifestEntry
from kdive.build_artifacts.results import BuildOutput, ValidatedUpload
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
from kdive.serialization import JsonValue

_NT_GNU_BUILD_ID = 3
_ELF_MAGIC = b"\x7fELF"
_ELF64LE_PREFIX = b"\x7fELF\x02\x01"  # magic + EI_CLASS=64-bit + EI_DATA=little-endian
_EM_PPC64_LE16 = (21).to_bytes(2, "little")  # e_machine == EM_PPC64, 16-bit LE at offset 0x12
_GZIP_MAGIC = b"\x1f\x8b"
_BZIMAGE_MAGIC = b"HdrS"
_BZIMAGE_MAGIC_OFFSET = 0x202
_SHT_NOTE = 7
_NO_GNU_BUILD_ID_NOTE = "vmlinux carries no GNU build-id note"
_MAX_SECTION_BYTES = 16 * 1024 * 1024
# The effective_config readable/upload cap (1 MiB). This module owns the single canonical value;
# the upload-admission path (mcp uploads tool) imports it so the advertised cap, the admission gate,
# and the validation gate cannot drift (#769, ADR-0234 §5). Imports flow mcp -> build_artifacts.
EFFECTIVE_CONFIG_MAX_BYTES = 1024 * 1024

# The combined `kernel` artifact is a gzip tar of boot/vmlinuz + lib/modules/<ver>/ (ADR-0234 §2).
_KERNEL_BOOT_MEMBER = "boot/vmlinuz"
_MODULES_MEMBER_PREFIX = "lib/modules/"
# A real kernel module under lib/modules/<release>/ ends in one of these; a bare directory or a
# metadata file (modules.dep, modules.order) does not satisfy the requirement (#1273, ADR-0381).
_MODULE_SUFFIXES = (".ko", ".ko.xz", ".ko.gz", ".ko.zst")
# Bound on *decompressed* output the shape scan reads: boot/vmlinuz is the first member, so the
# first lib/modules header is reached only after the bzImage payload (tens of MB). The cap sits
# well above a real bzImage so a large-but-legal kernel passes, while a gzip bomb (tiny gzip →
# gigabytes of tar) is stopped here rather than decompressing unbounded.
_KERNEL_TAR_SCAN_MAX_BYTES = 128 * 1024 * 1024
_RANGE_CHUNK_BYTES = 4 * 1024 * 1024
_EXTERNAL_BOOT_INITRD_MAX_BYTES = 512 * 1024 * 1024
_EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES = 2 * 1024 * 1024 * 1024
_EXTERNAL_BOOT_ARCHIVE_MAX_MEMBERS = 200_000
_EXTERNAL_BOOT_ARCHIVE_MAX_BYTES = 8 * 1024 * 1024 * 1024
_EXTERNAL_BOOT_MEMBER_MAX_BYTES = 512 * 1024 * 1024
_EXTERNAL_BOOT_EXTENSION_MAX_BYTES = 1024 * 1024
_EXTERNAL_BOOT_DECODED_KERNEL_MAX_BYTES = 2 * 1024 * 1024 * 1024
_EXTERNAL_BOOT_ELF_METADATA_MAX_BYTES = 16 * 1024 * 1024
_EXTERNAL_BOOT_COMPRESSION_CANDIDATES_MAX = 64
_SHA256_PREFIX = "sha256:"
_KERNEL_RELEASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")


@dataclass(frozen=True, slots=True)
class MagicPin:
    """A magic-byte signature: lowercase-hex bytes expected at a fixed byte ``offset``."""

    offset: int
    hex: str

    def to_json(self) -> dict[str, JsonValue]:
        """Return a JSON-safe view of this magic pin."""
        return {"offset": self.offset, "hex": self.hex}


@dataclass(frozen=True, slots=True)
class FormatContract:
    """The byte-format contract for an artifact or a member inside a container artifact."""

    container: str
    magic: tuple[MagicPin, ...] = ()
    max_bytes: int | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Return a JSON-safe view; ``max_bytes`` is present only when a cap applies."""
        data: dict[str, JsonValue] = {
            "container": self.container,
            "magic": [pin.to_json() for pin in self.magic],
        }
        if self.max_bytes is not None:
            data["max_bytes"] = self.max_bytes
        return data


@dataclass(frozen=True, slots=True)
class LayoutMember:
    """One member inside a container artifact (e.g. a path inside the combined kernel tar).

    ``formats_by_arch`` gives a member a per-arch byte contract (e.g. the ``boot/vmlinuz``
    bzImage-vs-ELF split of #1145); a member with no format constraint leaves it unset.
    """

    path: str
    required: bool
    note: str
    formats_by_arch: Mapping[str, FormatContract] | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Return a JSON-safe view; ``formats_by_arch`` appears only when the member declares it."""
        data: dict[str, JsonValue] = {
            "path": self.path,
            "required": self.required,
            "note": self.note,
        }
        if self.formats_by_arch is not None:
            data["formats_by_arch"] = {
                arch: fmt.to_json() for arch, fmt in self.formats_by_arch.items()
            }
        return data


@dataclass(frozen=True, slots=True)
class ArtifactContract:
    """The full upload contract for one externally uploaded build artifact (#769, ADR-0234 §5)."""

    name: str
    requirement: Literal["required", "optional"]
    summary: str
    format: FormatContract
    layout: tuple[LayoutMember, ...] = ()
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, JsonValue]:
        """Return a JSON-safe view; ``layout`` and ``notes`` are present only when non-empty."""
        data: dict[str, JsonValue] = {
            "name": self.name,
            "requirement": self.requirement,
            "summary": self.summary,
            "format": self.format.to_json(),
        }
        if self.layout:
            data["layout"] = [member.to_json() for member in self.layout]
        if self.notes:
            data["notes"] = list(self.notes)
        return data


# The per-arch boot/vmlinuz member format (#1145, ADR-0343): the single source both the validator
# and the external-build contract resource, so they cannot drift. x86_64 is the bzImage HdrS
# magic; ppc64le (powerpc has no bzImage) is an ELF64-LE kernel pinned to EM_PPC64 at e_machine so
# a non-ppc64 ELF64-LE (x86_64/aarch64 vmlinux, same \x7fELF\x02\x01 prefix) cannot leak in.
BOOT_MEMBER_FORMATS: Mapping[str, FormatContract] = {
    "x86_64": FormatContract(
        container="bzImage",
        magic=(MagicPin(offset=_BZIMAGE_MAGIC_OFFSET, hex=_BZIMAGE_MAGIC.hex()),),
    ),
    "ppc64le": FormatContract(
        container="ppc64le ELF (vmlinux)",
        magic=(
            MagicPin(offset=0, hex=_ELF64LE_PREFIX.hex()),
            MagicPin(offset=0x12, hex=_EM_PPC64_LE16.hex()),
        ),
    ),
}

# The profile-parse gate (SUPPORTED_ARCHES) and this payload-format gate must agree on the arch
# vocabulary; otherwise a create-accepted arch would finalize-reject after a full upload. This is a
# loud import-time failure if a future arch is added to one table but not the other.
if set(BOOT_MEMBER_FORMATS) != SUPPORTED_ARCHES:
    raise RuntimeError(
        "BOOT_MEMBER_FORMATS must cover exactly SUPPORTED_ARCHES; "
        f"got {sorted(BOOT_MEMBER_FORMATS)} vs {sorted(SUPPORTED_ARCHES)}"
    )


# The provider-neutral external-build upload contract, keyed by artifact name (ADR-0234 §5). The
# byte details (magic, layout member paths, the effective_config cap) are taken from this module's
# own validator constants, so the advertised contract cannot drift from what the validator enforces.
EXTERNAL_BUILD_CONTRACTS: Mapping[str, ArtifactContract] = {
    "kernel": ArtifactContract(
        name="kernel",
        requirement="required",
        summary=(
            "Combined kernel+modules tar (gzip): boot/vmlinuz (the bzImage for x86_64, the ELF "
            "vmlinux for ppc64le - the arch is declared in the build profile) plus "
            "lib/modules/<release>/. One artifact for both; there is no separate 'modules' upload."
        ),
        format=FormatContract(
            container="gzip tar",
            magic=(MagicPin(offset=0, hex=_GZIP_MAGIC.hex()),),
        ),
        layout=(
            LayoutMember(
                path=_KERNEL_BOOT_MEMBER,
                required=True,
                note=(
                    "The bootable kernel renamed to boot/vmlinuz: the bzImage "
                    "(arch/x86/boot/bzImage) for x86_64, or the stripped ELF vmlinux for ppc64le "
                    "(powerpc has no bzImage). The format is keyed by the build profile's arch."
                ),
                formats_by_arch=BOOT_MEMBER_FORMATS,
            ),
            LayoutMember(
                path=_MODULES_MEMBER_PREFIX,
                required=True,
                note=(
                    "The `make modules_install` tree: one or more lib/modules/<release>/ dirs "
                    "holding at least one real kernel module file (a *.ko, .ko.xz, .ko.gz, or "
                    ".ko.zst under lib/modules/<release>/); a bare directory or a modules.dep "
                    "with no module is rejected. Exclude the `build` and `source` "
                    "back-reference symlinks."
                ),
            ),
        ),
        notes=(
            "Must be gzip specifically; a plain .tar, .tar.xz, or .tar.zst is rejected.",
            "List boot/vmlinuz before lib/modules: validation scans at most the first 128 MiB of "
            "decompressed output (a gzip-bomb guard), so the lib/modules header must be within it.",
            "Finalization then scans the complete exact object version: at most 200000 headers, "
            "8589934592 uncompressed regular-file bytes, and 536870912 boot/vmlinuz bytes. "
            "It rejects aliases, duplicate paths, unrelated members, unsafe links, and nodes "
            "other than directories, regular files, and contained relative symlinks.",
        ),
    ),
    "vmlinux": ArtifactContract(
        name="vmlinux",
        requirement="optional",
        summary="Uncompressed kernel ELF with DWARF debug info; enables kernel debugging.",
        format=FormatContract(
            container="ELF (uncompressed)",
            magic=(MagicPin(offset=0, hex=_ELF_MAGIC.hex()),),
        ),
        notes=(
            "If uploaded you MUST pass a matching build_id to runs.complete_build; it must equal "
            "the ELF's GNU build-id note (e.g. from `readelf -n vmlinux`), or it is rejected.",
        ),
    ),
    "initrd": ArtifactContract(
        name="initrd",
        requirement="optional",
        summary="Initial ramdisk / initramfs image; upload when boot needs a specific initramfs.",
        format=FormatContract(
            container="initramfs image", max_bytes=_EXTERNAL_BOOT_INITRD_MAX_BYTES
        ),
        notes=(
            "Finalization streams and hashes the exact object version and pairs it with the "
            "kernel generation; exceeding the byte cap fails before publication.",
        ),
    ),
    "effective_config": ArtifactContract(
        name="effective_config",
        requirement="optional",
        summary="The kernel .config used for the build.",
        format=FormatContract(
            container="kernel .config (text)",
            max_bytes=EFFECTIVE_CONFIG_MAX_BYTES,
        ),
        notes=(
            "Optional and never rejected: kdive stores the .config verbatim and completing a build "
            "never fails over it. If you upload one, kdive does read it to emit a non-blocking "
            "advisory when it provably lacks the symbols needed to mount the root filesystem and "
            "boot (root=/dev/vda ext4 on virtio-blk); see "
            "resource://kdive/contracts/external-build.",
        ),
    ),
}


class ValidatorStore(HeadStore, Protocol):
    """Object-store operations needed by external build validation."""

    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes: ...


class _BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _ObservedVersionStore:
    """Fence semantic reads to the immutable version returned by the first HEAD."""

    def __init__(self, store: ValidatorStore) -> None:
        self._store = store
        self._versions: dict[str, str] = {}

    def head(self, key: str) -> HeadResult | None:
        head = self._store.head(key)
        if head is not None:
            self._versions[key] = head.version_id
        return head

    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes:
        del version_id
        return self._store.get_range(
            key, start=start, length=length, version_id=self._versions[key]
        )


def parse_gnu_build_id(notes: bytes) -> str:
    """Extract the GNU build-id (lowercase hex) from a little-endian ELF note blob."""
    offset = 0
    end = len(notes)
    while offset + 12 <= end:
        namesz = int.from_bytes(notes[offset : offset + 4], "little")
        descsz = int.from_bytes(notes[offset + 4 : offset + 8], "little")
        note_type = int.from_bytes(notes[offset + 8 : offset + 12], "little")
        name_start = offset + 12
        name_end = name_start + namesz
        desc_start = name_end + (-namesz % 4)
        desc_end = desc_start + descsz
        if desc_end > end:
            break
        name = notes[name_start:name_end].rstrip(b"\x00")
        if note_type == _NT_GNU_BUILD_ID and name == b"GNU":
            return notes[desc_start:desc_end].hex()
        next_offset = desc_end + (-descsz % 4)
        if next_offset <= offset:
            break
        offset = next_offset
    raise _build_failure(_NO_GNU_BUILD_ID_NOTE)


def validate_external_artifacts(
    store: ValidatorStore,
    *,
    manifest: Sequence[ManifestEntry],
    keys: Mapping[str, str],
    declared_build_id: str | None,
    arch: str = "x86_64",
) -> ValidatedUpload:
    """Validate uploaded build artifacts; return the ``BuildOutput`` plus object heads.

    The kernel bytes and any uploaded ``vmlinux`` build-id are checked, but the uploaded
    ``effective_config`` is accepted verbatim and never inspected (no Kconfig validation).

    ``arch`` (default ``x86_64``) selects the ``boot/vmlinuz`` payload format (ADR-0343): a
    bzImage for ``x86_64``, an ``EM_PPC64`` ELF64-LE kernel for ``ppc64le``. An arch outside
    :data:`BOOT_MEMBER_FORMATS` fails fast ``CONFIGURATION_ERROR`` (a defensive backstop; the
    build-profile parse already gated it upstream).
    """
    boot_format = _resolve_boot_format(arch)
    store = _ObservedVersionStore(store)
    by_name = {e.name: e for e in manifest}
    if "kernel" not in by_name or "kernel" not in keys:
        raise CategorizedError(
            "external build is missing the required kernel artifact",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    heads: dict[str, HeadResult] = {}
    for name, entry in by_name.items():
        key = keys.get(name)
        if key is None:
            raise CategorizedError(
                f"declared artifact {name!r} has no upload key",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"name": name},
            )
        heads[name] = _validate_one_artifact(
            store, name, entry, key, boot_format=boot_format, arch=arch
        )

    build_id = ""
    if "vmlinux" in by_name:
        if not declared_build_id:
            raise CategorizedError(
                "a vmlinux upload requires a declared build_id",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        actual = extract_build_id_ranged(
            store, keys["vmlinux"], max_size=heads["vmlinux"].size_bytes
        )
        if actual != declared_build_id.lower():
            raise _build_failure("declared build_id does not match the uploaded vmlinux")
        build_id = actual

    output = BuildOutput(
        kernel_ref=keys["kernel"],
        debuginfo_ref=keys.get("vmlinux", ""),
        build_id=build_id,
    )
    evidence = _external_boot_evidence(store, keys=keys, heads=heads, arch=arch, build_id=build_id)
    return ValidatedUpload(output=output, heads=heads, external_boot_evidence=evidence)


def _external_boot_evidence(
    store: ValidatorStore,
    *,
    keys: Mapping[str, str],
    heads: Mapping[str, HeadResult],
    arch: str,
    build_id: str,
) -> dict[str, JsonValue]:
    """Produce server-owned, version-pinned external-boot evidence (ADR-0583)."""
    bundle_head = heads["kernel"]
    if bundle_head.size_bytes > _EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES:
        raise _build_failure(
            "kernel bundle exceeds the external-boot compressed byte limit",
            max_bytes=_EXTERNAL_BOOT_ARCHIVE_COMPRESSED_MAX_BYTES,
        )
    archive = _scan_external_boot_archive(store, keys["kernel"], bundle_head.size_bytes, arch)
    if build_id and archive["gnu_build_id"] != build_id:
        raise _build_failure(
            "uploaded vmlinux build_id does not match boot/vmlinuz",
            vmlinux_build_id=build_id,
            boot_build_id=archive["gnu_build_id"],
        )
    initrd: dict[str, JsonValue] | None = None
    initrd_head = heads.get("initrd")
    if initrd_head is not None:
        if initrd_head.size_bytes > _EXTERNAL_BOOT_INITRD_MAX_BYTES:
            raise _build_failure(
                "initrd exceeds the external-boot byte limit; rebuild a smaller initrd",
                name="initrd",
                max_bytes=_EXTERNAL_BOOT_INITRD_MAX_BYTES,
            )
        initrd = {
            "sha256": _digest_object(store, keys["initrd"], initrd_head.size_bytes),
            "size_bytes": initrd_head.size_bytes,
        }
    return {
        "schema": "external-boot-evidence-v1",
        "bundle_sha256": _digest_object(store, keys["kernel"], bundle_head.size_bytes),
        "initrd": initrd,
        "archive_member_count": archive["archive_member_count"],
        "archive_uncompressed_bytes": archive["archive_uncompressed_bytes"],
        "vmlinuz_sha256": archive["vmlinuz_sha256"],
        "vmlinuz_size_bytes": archive["vmlinuz_size_bytes"],
        "decoded_kernel_size_bytes": archive["decoded_kernel_size_bytes"],
        "elf_metadata_bytes": archive["elf_metadata_bytes"],
        "architecture": archive["architecture"],
        "release": archive["release"],
        "gnu_build_id": archive["gnu_build_id"],
        "gnu_build_id_size_bytes": archive["gnu_build_id_size_bytes"],
        "module_source_manifest": archive["module_source_manifest"],
        "module_member_count": archive["module_member_count"],
        "module_uncompressed_bytes": archive["module_uncompressed_bytes"],
    }


def _digest_object(store: ValidatorStore, key: str, size_bytes: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size_bytes:
        chunk = store.get_range(
            key, start=offset, length=min(_RANGE_CHUNK_BYTES, size_bytes - offset)
        )
        if not chunk:
            raise _build_failure("artifact ended before its recorded size", key=key)
        digest.update(chunk)
        offset += len(chunk)
    return _SHA256_PREFIX + digest.hexdigest()


class _RangedReader:
    def __init__(self, store: ValidatorStore, key: str, size: int) -> None:
        self._store = store
        self._key = key
        self._size = size
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if self._offset >= self._size:
            return b""
        length = self._size - self._offset if size < 0 else min(size, self._size - self._offset)
        data = self._store.get_range(self._key, start=self._offset, length=length)
        if not data:
            return b""
        if len(data) > length:
            raise _build_failure("object store range response exceeded the requested bound")
        self._offset += len(data)
        return data


def _scan_external_boot_archive(
    store: ValidatorStore, key: str, size_bytes: int, arch: str
) -> dict[str, JsonValue]:
    entries: list[dict[str, JsonValue]] = []
    releases: set[str] = set()
    names: set[str] = set()
    boot_digest: str | None = None
    boot: dict[str, JsonValue] | None = None
    boot_size = 0
    archive_bytes = 0
    module_bytes = 0
    module_members = 0
    member_count = 0
    _preflight_external_boot_archive(store, key, size_bytes)
    reader = _RangedReader(store, key, size_bytes)
    try:
        with tarfile.open(fileobj=cast("IO[bytes]", reader), mode="r|gz") as archive:
            for member_count, member in enumerate(archive, start=1):
                if member_count > _EXTERNAL_BOOT_ARCHIVE_MAX_MEMBERS:
                    raise _build_failure(
                        "kernel bundle exceeds the external-boot member limit",
                        max_members=_EXTERNAL_BOOT_ARCHIVE_MAX_MEMBERS,
                    )
                path = _canonical_tar_path(member)
                if path in names:
                    raise _build_failure("kernel bundle contains a duplicate member", path=path)
                names.add(path)
                if member.isreg():
                    archive_bytes += member.size
                    if archive_bytes > _EXTERNAL_BOOT_ARCHIVE_MAX_BYTES:
                        raise _build_failure(
                            "kernel bundle exceeds the external-boot uncompressed byte limit",
                            max_bytes=_EXTERNAL_BOOT_ARCHIVE_MAX_BYTES,
                        )
                if path == _KERNEL_BOOT_MEMBER:
                    if not member.isreg() or boot_digest is not None:
                        raise _build_failure("boot/vmlinuz must be exactly one regular file")
                    if member.size > _EXTERNAL_BOOT_MEMBER_MAX_BYTES:
                        raise _build_failure(
                            "boot/vmlinuz exceeds the external-boot byte limit",
                            max_bytes=_EXTERNAL_BOOT_MEMBER_MAX_BYTES,
                        )
                    boot = _inspect_boot_member(archive, member, arch)
                    boot_digest = str(boot["vmlinuz_sha256"])
                    boot_size = member.size
                    continue
                module_path = _module_member_path(path, member)
                if module_path is None:
                    continue
                release, relative = module_path
                releases.add(release)
                if not relative:
                    continue
                if member.isreg():
                    module_bytes += member.size
                entry = _module_manifest_entry(archive, member, relative)
                if entry is not None:
                    entries.append(entry)
                    module_members += 1
    except (OSError, tarfile.TarError) as exc:
        raise _build_failure("kernel bundle is not a complete readable gzip tar") from exc
    if boot_digest is None or boot is None:
        raise _build_failure("kernel bundle has no regular boot/vmlinuz member")
    if len(releases) != 1 or not entries:
        raise _build_failure("kernel bundle must contain exactly one lib/modules/<release> tree")
    _validate_module_topology(entries)
    module_release = next(iter(releases))
    if boot["release"] != module_release:
        raise _build_failure(
            "boot/vmlinuz release does not match its module tree",
            kernel_release=boot["release"],
            module_release=module_release,
        )
    entries.sort(key=lambda entry: str(entry["path"]).encode())
    document = {"entries": entries, "schema": "module-source-manifest-v1"}
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest = hashlib.sha256(b"kdive-module-source-manifest-v1\0" + encoded.encode()).hexdigest()
    return {
        "archive_member_count": member_count,
        "archive_uncompressed_bytes": archive_bytes,
        "vmlinuz_sha256": boot_digest,
        "vmlinuz_size_bytes": boot_size,
        "decoded_kernel_size_bytes": boot["decoded_kernel_size_bytes"],
        "elf_metadata_bytes": boot["elf_metadata_bytes"],
        "architecture": boot["architecture"],
        "kernel_release": boot["release"],
        "gnu_build_id": boot["gnu_build_id"],
        "gnu_build_id_size_bytes": boot["gnu_build_id_size_bytes"],
        "release": module_release,
        "module_source_manifest": _SHA256_PREFIX + manifest,
        "module_member_count": module_members,
        "module_uncompressed_bytes": module_bytes,
    }


def _preflight_external_boot_archive(store: ValidatorStore, key: str, size_bytes: int) -> None:
    """Bound raw tar work before ``tarfile`` consumes GNU/PAX extension payloads."""
    reader = _RangedReader(store, key, size_bytes)
    raw_bytes = 0
    headers = 0
    try:
        with gzip.GzipFile(fileobj=cast("IO[bytes]", reader), mode="rb") as source:
            while True:
                header = source.read(tarfile.BLOCKSIZE)
                if not header:
                    return
                if len(header) != tarfile.BLOCKSIZE:
                    raise _build_failure("kernel bundle has a truncated tar header")
                raw_bytes += len(header)
                if header == tarfile.NUL * tarfile.BLOCKSIZE:
                    return
                headers += 1
                if headers > _EXTERNAL_BOOT_ARCHIVE_MAX_MEMBERS:
                    raise _build_failure(
                        "kernel bundle exceeds the external-boot member limit",
                        max_members=_EXTERNAL_BOOT_ARCHIVE_MAX_MEMBERS,
                    )
                member = tarfile.TarInfo.frombuf(header, "utf-8", "surrogateescape")
                extension_types = {
                    tarfile.XHDTYPE,
                    tarfile.XGLTYPE,
                    tarfile.GNUTYPE_LONGNAME,
                    tarfile.GNUTYPE_LONGLINK,
                }
                if (
                    member.type in extension_types
                    and member.size > _EXTERNAL_BOOT_EXTENSION_MAX_BYTES
                ):
                    raise _build_failure(
                        "kernel bundle extension metadata exceeds the byte limit",
                        max_bytes=_EXTERNAL_BOOT_EXTENSION_MAX_BYTES,
                    )
                blocks = (member.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
                padded_size = blocks * tarfile.BLOCKSIZE
                raw_bytes += padded_size
                if raw_bytes > _EXTERNAL_BOOT_ARCHIVE_MAX_BYTES:
                    raise _build_failure(
                        "kernel bundle exceeds the external-boot raw tar byte limit",
                        max_bytes=_EXTERNAL_BOOT_ARCHIVE_MAX_BYTES,
                    )
                _discard_exact(source, padded_size)
    except (OSError, tarfile.TarError) as exc:
        raise _build_failure("kernel bundle is not a complete readable gzip tar") from exc


def _discard_exact(source: _BinaryReader, size: int) -> None:
    remaining = size
    while remaining:
        chunk = source.read(min(_RANGE_CHUNK_BYTES, remaining))
        if not chunk:
            raise _build_failure("kernel bundle ended before its recorded member size")
        remaining -= len(chunk)


def _canonical_tar_path(member: tarfile.TarInfo) -> str:
    value = member.name
    if member.isdir() and value.endswith("/"):
        value = value[:-1]
    if not value or value.startswith(("/", "./")) or "\\" in value:
        raise _build_failure("kernel bundle contains a noncanonical member path", path=value)
    normalized = posixpath.normpath(value)
    if normalized != value or normalized in {".", ".."} or normalized.startswith("../"):
        raise _build_failure("kernel bundle contains a noncanonical member path", path=value)
    if unicodedata.normalize("NFC", value) != value:
        raise _build_failure("kernel bundle member paths must be NFC", path=value)
    return value


def _module_member_path(path: str, member: tarfile.TarInfo) -> tuple[str, str] | None:
    if path in {"lib", "lib/modules"}:
        if member.isdir():
            return None
        raise _build_failure("kernel bundle module ancestors must be directories", path=path)
    if not path.startswith(_MODULES_MEMBER_PREFIX):
        raise _build_failure("kernel bundle contains an unrelated member", path=path)
    remainder = path[len(_MODULES_MEMBER_PREFIX) :]
    release, separator, relative = remainder.partition("/")
    if release and not separator and member.isdir():
        return release, ""
    if not release or not separator or not relative:
        raise _build_failure("kernel bundle has an invalid module-tree member", path=path)
    return release, relative


def _module_manifest_entry(
    archive: tarfile.TarFile, member: tarfile.TarInfo, relative: str
) -> dict[str, JsonValue] | None:
    mode = member.mode & 0o777
    if member.isdir():
        return {"mode": "0755", "path": relative, "type": "dir"}
    if member.isreg():
        extracted = archive.extractfile(member)
        if extracted is None:
            raise _build_failure("module file cannot be read", path=relative)
        digest = hashlib.sha256()
        remaining = member.size
        while remaining:
            chunk = extracted.read(min(_RANGE_CHUNK_BYTES, remaining))
            if not chunk:
                raise _build_failure("module file ended before its recorded size", path=relative)
            digest.update(chunk)
            remaining -= len(chunk)
        return {
            "mode": "0755" if mode & 0o111 else "0644",
            "path": relative,
            "sha256": _SHA256_PREFIX + digest.hexdigest(),
            "size": member.size,
            "type": "file",
        }
    if member.issym():
        target = member.linkname
        try:
            target.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _build_failure("module symlink target is not UTF-8", path=relative) from exc
        if unicodedata.normalize("NFC", target) != target:
            raise _build_failure("module symlink target must be NFC", path=relative)
        if relative in {"build", "source"} and target.startswith("/"):
            return None
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
        if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
            raise _build_failure("module symlink escapes its release tree", path=relative)
        return {"mode": "0777", "path": relative, "target": target, "type": "symlink"}
    raise _build_failure("kernel bundle contains an unsupported module member", path=relative)


def _validate_module_topology(entries: Sequence[Mapping[str, JsonValue]]) -> None:
    types = {str(entry["path"]): str(entry["type"]) for entry in entries}
    for path in types:
        parts = path.split("/")
        for end in range(1, len(parts)):
            ancestor = "/".join(parts[:end])
            ancestor_type = types.get(ancestor)
            if ancestor_type is not None and ancestor_type != "dir":
                raise _build_failure(
                    "module-tree member has a non-directory ancestor",
                    path=path,
                    ancestor=ancestor,
                    ancestor_type=ancestor_type,
                )


def _inspect_boot_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, expected_arch: str
) -> dict[str, JsonValue]:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise _build_failure("boot/vmlinuz cannot be read")
    digest = hashlib.sha256()
    remaining = member.size
    with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as boot:
        while remaining:
            chunk = extracted.read(min(_RANGE_CHUNK_BYTES, remaining))
            if not chunk:
                raise _build_failure("boot/vmlinuz ended before its recorded size")
            digest.update(chunk)
            boot.write(chunk)
            remaining -= len(chunk)
        boot.seek(0)
        release = _boot_release(boot, expected_arch)
        decoded = _decoded_kernel(boot, expected_arch)
        with decoded:
            metadata = _elf_kernel_metadata(decoded, expected_arch, release)
        if metadata["release"] != release:
            raise _build_failure(
                "boot/vmlinuz release disagrees with its decoded kernel",
                header_release=release,
                decoded_release=metadata["release"],
            )
        return {
            "vmlinuz_sha256": _SHA256_PREFIX + digest.hexdigest(),
            "decoded_kernel_size_bytes": metadata["decoded_kernel_size_bytes"],
            "elf_metadata_bytes": metadata["elf_metadata_bytes"],
            "architecture": metadata["architecture"],
            "release": release,
            "gnu_build_id": metadata["gnu_build_id"],
            "gnu_build_id_size_bytes": metadata["gnu_build_id_size_bytes"],
        }


def _boot_release(boot: IO[bytes], arch: str) -> str:
    if arch == "x86_64":
        boot.seek(0)
        header = boot.read(0x300)
        magic = header[_BZIMAGE_MAGIC_OFFSET : _BZIMAGE_MAGIC_OFFSET + 4]
        if len(header) < 0x210 or magic != _BZIMAGE_MAGIC:
            raise _build_failure("boot/vmlinuz has no usable x86 boot header")
        version_offset = 0x200 + int.from_bytes(header[0x20E:0x210], "little")
        boot.seek(version_offset)
        release = boot.read(256).partition(b"\0")[0]
        return _validated_release(release)
    boot.seek(0)
    return _release_from_linux_banner(boot.read(_EXTERNAL_BOOT_ELF_METADATA_MAX_BYTES))


def _decoded_kernel(boot: IO[bytes], arch: str) -> tempfile.SpooledTemporaryFile[bytes]:
    # Ownership transfers to the caller, which closes the returned spool.
    decoded = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)  # noqa: SIM115
    budget = _DecodeBudget()
    if arch == "ppc64le":
        boot.seek(0)
        _copy_kernel_bounded(boot, decoded, budget)
        decoded.seek(0)
        return decoded
    candidates = (
        (b"\x1f\x8b\x08", lambda source: gzip.GzipFile(fileobj=source, mode="rb")),
        (b"BZh", lambda source: bz2.BZ2File(source)),
        (b"\xfd7zXZ\x00", _open_lzma),
        (b"\x28\xb5\x2f\xfd", _open_zstd),
    )
    candidate_count = 0
    for magic, opener in candidates:
        for offset in _magic_offsets(boot, magic):
            candidate_count += 1
            if candidate_count > _EXTERNAL_BOOT_COMPRESSION_CANDIDATES_MAX:
                decoded.close()
                raise _build_failure(
                    "boot/vmlinuz exceeds the compression-candidate work limit",
                    max_candidates=_EXTERNAL_BOOT_COMPRESSION_CANDIDATES_MAX,
                )
            boot.seek(offset)
            try:
                with opener(boot) as source:
                    _copy_kernel_bounded(source, decoded, budget)
            except EOFError, OSError, zlib.error, lzma.LZMAError, zstd.ZstdError:
                decoded.seek(0)
                decoded.truncate()
                continue
            decoded.seek(0)
            if decoded.read(4) == _ELF_MAGIC:
                decoded.seek(0)
                return decoded
            decoded.seek(0)
            decoded.truncate()
    decoded.close()
    raise _build_failure(
        "boot/vmlinuz does not contain a supported gzip, bzip2, xz, or zstd ELF kernel payload"
    )


def _magic_offsets(source: IO[bytes], magic: bytes) -> Iterator[int]:
    source.seek(0)
    overlap = b""
    position = 0
    count = 0
    while chunk := source.read(_RANGE_CHUNK_BYTES):
        data = overlap + chunk
        start = 0
        while (index := data.find(magic, start)) >= 0:
            count += 1
            if count > _EXTERNAL_BOOT_COMPRESSION_CANDIDATES_MAX:
                raise _build_failure(
                    "boot/vmlinuz exceeds the compression-candidate work limit",
                    max_candidates=_EXTERNAL_BOOT_COMPRESSION_CANDIDATES_MAX,
                )
            yield position - len(overlap) + index
            start = index + 1
        overlap = data[-(len(magic) - 1) :]
        position += len(chunk)


@dataclass(slots=True)
class _DecodeBudget:
    remaining: int = field(default_factory=lambda: _EXTERNAL_BOOT_DECODED_KERNEL_MAX_BYTES)


def _copy_kernel_bounded(
    source: _BinaryReader, destination: IO[bytes], budget: _DecodeBudget
) -> int:
    total = 0
    while chunk := source.read(min(_RANGE_CHUNK_BYTES, budget.remaining + 1)):
        total += len(chunk)
        budget.remaining -= len(chunk)
        if budget.remaining < 0:
            raise _build_failure(
                "decoded boot/vmlinuz exceeds the aggregate decompression work limit",
                max_bytes=_EXTERNAL_BOOT_DECODED_KERNEL_MAX_BYTES,
            )
        destination.write(chunk)
    return total


@dataclass(slots=True)
class _BoundedElfReader:
    source: IO[bytes]
    size: int
    intervals: list[tuple[int, int]] = field(default_factory=list)

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise _build_failure("decoded boot/vmlinuz ELF metadata extends past the object")
        intervals = [*self.intervals, (offset, offset + length)]
        measured = _interval_bytes(intervals)
        if measured > _EXTERNAL_BOOT_ELF_METADATA_MAX_BYTES:
            raise _build_failure(
                "decoded boot/vmlinuz exceeds the ELF metadata read limit",
                max_bytes=_EXTERNAL_BOOT_ELF_METADATA_MAX_BYTES,
            )
        self.source.seek(offset)
        data = self.source.read(length)
        if len(data) != length:
            raise _build_failure("decoded boot/vmlinuz ELF metadata is truncated")
        self.intervals = intervals
        return data

    @property
    def measured_bytes(self) -> int:
        return _interval_bytes(self.intervals)


def _interval_bytes(intervals: Sequence[tuple[int, int]]) -> int:
    total = 0
    end = 0
    for start, stop in sorted(intervals):
        if stop <= end:
            continue
        total += stop - max(start, end)
        end = stop
    return total


def _elf_kernel_metadata(
    kernel: IO[bytes], expected_arch: str, expected_release: str
) -> dict[str, JsonValue]:
    kernel.seek(0, io.SEEK_END)
    reader = _BoundedElfReader(kernel, kernel.tell())
    header = reader.read(0, 64)
    if header[:6] != _ELF64LE_PREFIX:
        raise _build_failure("decoded boot/vmlinuz is not a 64-bit little-endian ELF")
    machine = int.from_bytes(header[0x12:0x14], "little")
    expected_machine = 62 if expected_arch == "x86_64" else 21
    if machine != expected_machine:
        raise _build_failure(
            "decoded boot/vmlinuz architecture does not match the build profile",
            expected_arch=expected_arch,
            e_machine=machine,
        )
    program_headers = _elf_program_headers(reader, header)
    build_ids: set[str] = set()
    for segment_type, offset, size in program_headers:
        if offset + size > reader.size:
            raise _build_failure("decoded boot/vmlinuz ELF segment extends past the object")
        if segment_type == 4:  # PT_NOTE
            build_ids.update(_gnu_build_ids_from_notes(reader.read(offset, size)))
    decoded_release: str | None = None
    for segment_type, offset, size in program_headers:
        if segment_type != 1 or decoded_release is not None:  # PT_LOAD
            continue
        remaining = _EXTERNAL_BOOT_ELF_METADATA_MAX_BYTES - reader.measured_bytes
        if remaining <= 0:
            break
        decoded_release = _optional_linux_release(reader.read(offset, min(size, remaining)))
    if decoded_release is not None and decoded_release != expected_release:
        raise _build_failure(
            "boot/vmlinuz release disagrees with its decoded kernel",
            header_release=expected_release,
            decoded_release=decoded_release,
        )
    if len(build_ids) != 1:
        raise _build_failure(
            "decoded boot/vmlinuz must contain one unambiguous GNU build ID",
            build_id_count=len(build_ids),
        )
    build_id = next(iter(build_ids))
    if not 4 <= len(build_id) // 2 <= 64:
        raise _build_failure("decoded boot/vmlinuz GNU build ID has an invalid byte length")
    return {
        "decoded_kernel_size_bytes": reader.size,
        "elf_metadata_bytes": reader.measured_bytes,
        "architecture": expected_arch,
        "release": expected_release,
        "gnu_build_id": build_id,
        "gnu_build_id_size_bytes": len(build_id) // 2,
    }


def _elf_program_headers(reader: _BoundedElfReader, header: bytes) -> list[tuple[int, int, int]]:
    offset = struct.unpack_from("<Q", header, 0x20)[0]
    entry_size = struct.unpack_from("<H", header, 0x36)[0]
    count = struct.unpack_from("<H", header, 0x38)[0]
    if offset == 0 or count == 0 or entry_size < 56:
        raise _build_failure("decoded boot/vmlinuz has no usable ELF program header table")
    table = reader.read(offset, entry_size * count)
    result: list[tuple[int, int, int]] = []
    for index in range(count):
        base = index * entry_size
        result.append(
            (
                struct.unpack_from("<I", table, base)[0],
                struct.unpack_from("<Q", table, base + 8)[0],
                struct.unpack_from("<Q", table, base + 32)[0],
            )
        )
    return result


def _release_from_linux_banner(data: bytes) -> str:
    release = _optional_linux_release(data)
    if release is None:
        raise _build_failure("decoded boot/vmlinuz has no bounded Linux release banner")
    return release


def _optional_linux_release(data: bytes) -> str | None:
    marker = b"Linux version "
    start = data.find(marker)
    if start < 0:
        return None
    release = data[start + len(marker) :].split(maxsplit=1)[0]
    return _validated_release(release)


def _validated_release(value: bytes) -> str:
    try:
        release = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _build_failure("boot/vmlinuz release is not UTF-8") from exc
    if _KERNEL_RELEASE_RE.fullmatch(release) is None:
        raise _build_failure("boot/vmlinuz release is not canonical")
    return release


def _open_lzma(source: IO[bytes]) -> lzma.LZMAFile:
    return lzma.LZMAFile(source)  # noqa: SIM115


def _open_zstd(source: IO[bytes]) -> zstd.ZstdFile:
    return zstd.ZstdFile(source, mode="rb")  # noqa: SIM115


def _gnu_build_ids_from_notes(data: bytes) -> set[str]:
    found: set[str] = set()
    offset = 0
    while offset + 12 <= len(data):
        namesz, descsz, note_type = struct.unpack_from("<III", data, offset)
        name_start = offset + 12
        name_end = name_start + namesz
        desc_start = name_end + (-namesz % 4)
        desc_end = desc_start + descsz
        if desc_end > len(data):
            raise _build_failure("decoded boot/vmlinuz has a malformed ELF note segment")
        if note_type == _NT_GNU_BUILD_ID and data[name_start:name_end].rstrip(b"\0") == b"GNU":
            found.add(data[desc_start:desc_end].hex())
        offset = desc_end + (-descsz % 4)
    return found


def extract_build_id_ranged(store: ValidatorStore, key: str, *, max_size: int) -> str:
    """Extract a vmlinux GNU build-id via bounded ranged ELF64-LE reads."""
    header = store.get_range(key, start=0, length=64)
    if len(header) < 64:
        raise _build_failure("vmlinux ELF header is truncated")
    if header[:4] != _ELF_MAGIC or header[4] != 2 or header[5] != 1:
        raise _build_failure("vmlinux is not a 64-bit little-endian ELF")
    try:
        e_shoff = struct.unpack_from("<Q", header, 0x28)[0]
        e_shentsize = struct.unpack_from("<H", header, 0x3A)[0]
        e_shnum = struct.unpack_from("<H", header, 0x3C)[0]
        if e_shoff == 0 or e_shnum == 0 or e_shentsize < 64:
            raise _build_failure("vmlinux has no usable section header table")
        if e_shentsize * e_shnum > _MAX_SECTION_BYTES:
            raise _build_failure(
                "vmlinux section header table exceeds the readable cap",
                sht_bytes=e_shentsize * e_shnum,
            )
        if e_shoff + e_shentsize * e_shnum > max_size:
            raise _build_failure("vmlinux section header table extends past the object size")
        sht = store.get_range(key, start=e_shoff, length=e_shentsize * e_shnum)
        return _find_build_id_note(store, key, sht, e_shentsize, e_shnum, max_size=max_size)
    except (struct.error, ValueError, IndexError) as exc:
        raise _build_failure("vmlinux ELF is structurally malformed") from exc


def _resolve_boot_format(arch: str) -> FormatContract:
    """Resolve the ``boot/vmlinuz`` format for ``arch``, failing fast on an unknown arch."""
    boot_format = BOOT_MEMBER_FORMATS.get(arch)
    if boot_format is None:
        supported = ", ".join(sorted(BOOT_MEMBER_FORMATS))
        raise CategorizedError(
            f"unsupported build arch; expected one of {supported}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return boot_format


def _validate_one_artifact(
    store: ValidatorStore,
    name: str,
    entry: ManifestEntry,
    key: str,
    *,
    boot_format: FormatContract,
    arch: str,
) -> HeadResult:
    head = store.head(key)
    if head is None:
        raise CategorizedError(
            f"declared artifact {name!r} was never uploaded",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": name},
        )
    if entry.chunks is None:
        if head.checksum_sha256 is None:
            # A single-PUT object with no stored SHA-256 was not written through the
            # presigned PUT (which signs x-amz-checksum-sha256 onto the object). A direct
            # put_object that skips that header stores the bytes but no checksum, so name that
            # cause instead of the generic "disagrees" message (#1338).
            raise _build_failure(
                "uploaded artifact has no stored SHA-256 checksum (the upload bypassed the "
                "presigned PUT; a direct put_object must send the x-amz-checksum-sha256 header)",
                name=name,
            )
        if head.size_bytes != entry.size_bytes or head.checksum_sha256 != entry.sha256:
            raise _build_failure("uploaded artifact disagrees with its manifest", name=name)
    elif head.size_bytes != entry.size_bytes:
        # The reassembled multipart object exposes only a composite checksum, so the
        # whole-object SHA-256 is not comparable here; the per-chunk pins (verify_chunks)
        # already bound every byte. Only the total size is checked on the final object.
        raise _build_failure("reassembled artifact size disagrees with its manifest", name=name)
    _check_artifact_content(store, name, key, head.size_bytes, boot_format=boot_format, arch=arch)
    return head


def _check_artifact_content(
    store: ValidatorStore,
    name: str,
    key: str,
    size_bytes: int,
    *,
    boot_format: FormatContract,
    arch: str,
) -> None:
    if name == "vmlinux":
        if store.get_range(key, start=0, length=4) != _ELF_MAGIC:
            raise _build_failure("vmlinux is not an ELF file", name=name)
    elif name == "kernel":
        _check_kernel_combined_tar(
            store, key, name, size_bytes=size_bytes, boot_format=boot_format, arch=arch
        )


def _check_kernel_combined_tar(
    store: ValidatorStore,
    key: str,
    name: str,
    *,
    size_bytes: int,
    boot_format: FormatContract,
    arch: str,
) -> None:
    """Validate the external `kernel` upload is a combined kernel+modules tar (ADR-0234 §2).

    The artifact must be a gzip stream whose tar holds ``boot/vmlinuz`` (matching ``boot_format``
    for the declared arch — a bzImage for x86_64, an ELF kernel for ppc64le) and at least one real
    kernel-module file under ``lib/modules/<release>/`` (a ``*.ko``/``.ko.xz``/``.ko.gz``/
    ``.ko.zst``). The scan decompresses at most :data:`_KERNEL_TAR_SCAN_MAX_BYTES` so a gzip bomb
    cannot make this read unbounded; if both members are not seen within that bound the upload is
    rejected. A stream that ends below the cap without reaching its gzip trailer — or with a corrupt
    CRC/ISIZE trailer — is rejected as truncated/corrupt rather than silently accepted (#1273).
    """
    if store.get_range(key, start=0, length=2) != _GZIP_MAGIC:
        raise _build_failure("kernel artifact is not a gzip-compressed combined tar", name=name)
    data, cap_reached, gzip_complete = _decompress_bounded(
        store, key, name, total_size=size_bytes, max_out=_KERNEL_TAR_SCAN_MAX_BYTES
    )
    if not cap_reached and not gzip_complete:
        # The stream ended below the scan cap without reaching a clean gzip EOF: the trailer is
        # missing, so the archive was truncated in transit or at the source (#1273, ADR-0381).
        # Over the cap this is not decidable without unbounded decompression, so it is only a
        # signal below it — exactly where the gzip-bomb guard is not engaged.
        raise _build_failure(
            "kernel artifact gzip stream is truncated: it ended before the gzip trailer, so the "
            "combined tar is incomplete; re-upload the full archive",
            name=name,
        )
    _verify_combined_tar_shape(data, name, boot_format, cap_reached=cap_reached, arch=arch)


def _decompress_bounded(
    store: ValidatorStore, key: str, name: str, *, total_size: int, max_out: int
) -> tuple[bytes, bool, bool]:
    """Gunzip ``key`` via sequential ranged reads, stopping at ``max_out`` decompressed bytes.

    Returns ``(data, cap_reached, gzip_complete)``: the decompressed prefix, ``cap_reached``
    (``True`` when the ``max_out`` bound cut the stream short rather than reaching a clean gzip
    EOF), and ``gzip_complete`` (``decompressor.eof`` — the gzip trailer was reached and its
    CRC/ISIZE verified). A corrupt trailer makes ``zlib`` raise, which is categorized here as a
    build failure rather than surfacing as an uncategorized error (#1273, ADR-0381).
    """
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)  # 16 + MAX_WBITS selects gzip framing
    out = bytearray()
    offset = 0
    while offset < total_size and len(out) < max_out:
        length = min(_RANGE_CHUNK_BYTES, total_size - offset)
        chunk = store.get_range(key, start=offset, length=length)
        if not chunk:
            break
        offset += len(chunk)
        try:
            out += decompressor.decompress(chunk, max_out - len(out))
        except zlib.error as exc:
            raise _build_failure(
                "kernel artifact gzip stream is corrupt: decompression failed; re-upload the "
                "archive",
                name=name,
            ) from exc
        if decompressor.eof:
            break
    return bytes(out), len(out) >= max_out, decompressor.eof


# Appended to the scan-bound rejection only for the arch whose unstripped kernel image is large
# enough to overrun the scan window (#1339): powerpc has no bzImage, so its boot member is the ELF
# `vmlinux`, and an unstripped vmlinux carries full DWARF (hundreds of MB) that pushes lib/modules
# past the bound. x86_64's bzImage is already stripped/compressed, so the generic hint suffices.
_PPC64LE_STRIP_HINT = (
    " (ppc64le: strip the build-tree vmlinux before packaging - see "
    "docs/operating/external-build-upload.md)"
)


def _scan_bound_rejection_message(arch: str) -> str:
    """Build the oversized-boot-member rejection, naming the scan bound and an arch-gated remedy.

    The scan stops at :data:`_KERNEL_TAR_SCAN_MAX_BYTES` (a gzip-bomb guard), so the boot member's
    own decompressed size is never measured -- the message states the bound that was hit, not a
    fabricated member size (#1339). The ppc64le strip pointer fires only for ``ppc64le``.
    """
    mib = _KERNEL_TAR_SCAN_MAX_BYTES // (1024 * 1024)
    hint = _PPC64LE_STRIP_HINT if arch == "ppc64le" else ""
    return (
        f"kernel combined tar boot/vmlinuz exceeds the {mib} MiB scan bound before any lib/modules "
        "member (the scan stops at the bound, so the boot member's full decompressed size is not "
        "measured); strip the boot image or list lib/modules earlier" + hint
    )


def _verify_combined_tar_shape(
    data: bytes, name: str, boot_format: FormatContract, *, cap_reached: bool, arch: str
) -> None:
    boot_seen = False
    boot_ok = False
    modules_ok = False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                path = _normalized_member_name(member.name)
                if path == _KERNEL_BOOT_MEMBER and member.isfile():
                    boot_seen = True
                    boot_ok = _member_matches_format(archive, member, boot_format)
                elif _is_kernel_module_member(path, member):
                    modules_ok = True
                if boot_ok and modules_ok:
                    break
    except tarfile.TarError as exc:
        # An open failure (not a tar at all) is fatal; a truncation mid-iteration is the expected
        # outcome when the decompress bound cut the tail — fall through to the member checks so a
        # gzip bomb surfaces as a precise "no lib/modules within the scan bound". Content integrity
        # of a complete (sub-cap) gzip is already guaranteed by its CRC/ISIZE trailer, verified in
        # _decompress_bounded, so a mid-stream corruption never reaches here as a valid tar.
        if not (boot_ok or modules_ok):
            raise _build_failure("kernel artifact is not a readable tar", name=name) from exc
    if not boot_ok:
        # Distinguish "member absent" from "member present but wrong arch" — the latter is the
        # #1145 arch cross-check (a plausible operator mistake: right file, wrong build arch), and
        # a "has no ... member" message would misdirect them to look for a missing file.
        if boot_seen:
            raise _build_failure(
                f"kernel combined tar boot/vmlinuz is present but is not a "
                f"{boot_format.container} member for the declared arch",
                name=name,
            )
        raise _build_failure(
            f"kernel combined tar has no boot/vmlinuz {boot_format.container} member", name=name
        )
    if not modules_ok:
        if cap_reached:
            raise _build_failure(_scan_bound_rejection_message(arch), name=name)
        raise _build_failure(
            "kernel combined tar has no lib/modules member within the scan bound", name=name
        )


def _member_matches_format(
    archive: tarfile.TarFile, member: tarfile.TarInfo, boot_format: FormatContract
) -> bool:
    """Whether ``member``'s bytes satisfy every magic pin of ``boot_format`` (all required)."""
    extracted = archive.extractfile(member)
    if extracted is None:
        return False
    pins = [(pin.offset, bytes.fromhex(pin.hex)) for pin in boot_format.magic]
    if not pins:
        return False
    head = extracted.read(max(offset + len(want) for offset, want in pins))
    return all(head[offset : offset + len(want)] == want for offset, want in pins)


def _is_kernel_module_member(path: str, member: tarfile.TarInfo) -> bool:
    """Whether ``path`` is a real kernel-module file under ``lib/modules/<release>/`` (#1273).

    A bare ``lib/modules/`` directory member or a metadata file (``modules.dep``) satisfied the
    old shallow prefix match; the requirement is now a regular file at
    ``lib/modules/<release>/…`` whose name ends in ``.ko``/``.ko.xz``/``.ko.gz``/``.ko.zst``.
    """
    if not member.isfile() or not path.startswith(_MODULES_MEMBER_PREFIX):
        return False
    remainder = path[len(_MODULES_MEMBER_PREFIX) :]
    if "/" not in remainder:  # need a <release>/ segment before the module file
        return False
    return path.endswith(_MODULE_SUFFIXES)


def _normalized_member_name(name: str) -> str:
    if name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def _find_build_id_note(
    store: ValidatorStore,
    key: str,
    sht: bytes,
    e_shentsize: int,
    e_shnum: int,
    *,
    max_size: int,
) -> str:
    for i in range(e_shnum):
        off = i * e_shentsize
        sh_type = struct.unpack_from("<I", sht, off + 4)[0]
        if sh_type != _SHT_NOTE:
            continue
        notes = _read_section(store, key, sht, e_shentsize, i, max_size=max_size)
        try:
            return parse_gnu_build_id(notes)
        except CategorizedError as exc:
            if _is_missing_build_id_note(exc):
                continue
            raise
    raise _build_failure(_NO_GNU_BUILD_ID_NOTE)


def _read_section(
    store: ValidatorStore, key: str, sht: bytes, e_shentsize: int, index: int, *, max_size: int
) -> bytes:
    off = index * e_shentsize
    sh_offset = struct.unpack_from("<Q", sht, off + 0x18)[0]
    sh_size = struct.unpack_from("<Q", sht, off + 0x20)[0]
    if sh_size > _MAX_SECTION_BYTES:
        raise _build_failure("vmlinux section exceeds the readable-section cap", sh_size=sh_size)
    if sh_offset + sh_size > max_size:
        raise _build_failure(
            "vmlinux section extends past the object size", sh_offset=sh_offset, sh_size=sh_size
        )
    return store.get_range(key, start=sh_offset, length=sh_size)


def _build_failure(message: str, **details: object) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.BUILD_FAILURE, details=details)


def _is_missing_build_id_note(exc: CategorizedError) -> bool:
    return exc.category is ErrorCategory.BUILD_FAILURE and str(exc) == _NO_GNU_BUILD_ID_NOTE
