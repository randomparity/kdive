"""Provider-neutral validation for externally uploaded build artifacts."""

from __future__ import annotations

import io
import struct
import tarfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from kdive.artifacts.chunks import HeadStore
from kdive.artifacts.storage import HeadResult
from kdive.artifacts.uploads import ManifestEntry
from kdive.build_artifacts.results import BuildOutput, ValidatedUpload
from kdive.components.requirements import ConfigRequirements, validate_config_requirements
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.serialization import JsonValue

_NT_GNU_BUILD_ID = 3
_ELF_MAGIC = b"\x7fELF"
_GZIP_MAGIC = b"\x1f\x8b"
_BZIMAGE_MAGIC = b"HdrS"
_BZIMAGE_MAGIC_OFFSET = 0x202
_SHT_NOTE = 7
_MAX_SECTION_BYTES = 16 * 1024 * 1024
# The effective_config readable/upload cap (1 MiB). This module owns the single canonical value;
# the upload-admission path (mcp uploads tool) imports it so the advertised cap, the admission gate,
# and the validation gate cannot drift (#769, ADR-0234 §5). Imports flow mcp -> build_artifacts.
EFFECTIVE_CONFIG_MAX_BYTES = 1024 * 1024

# The combined `kernel` artifact is a gzip tar of boot/vmlinuz + lib/modules/<ver>/ (ADR-0234 §2).
_KERNEL_BOOT_MEMBER = "boot/vmlinuz"
_MODULES_MEMBER_PREFIX = "lib/modules/"
# Bound on *decompressed* output the shape scan reads: boot/vmlinuz is the first member, so the
# first lib/modules header is reached only after the bzImage payload (tens of MB). The cap sits
# well above a real bzImage so a large-but-legal kernel passes, while a gzip bomb (tiny gzip →
# gigabytes of tar) is stopped here rather than decompressing unbounded.
_KERNEL_TAR_SCAN_MAX_BYTES = 128 * 1024 * 1024
_RANGE_CHUNK_BYTES = 4 * 1024 * 1024


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
    """One member inside a container artifact (e.g. a path inside the combined kernel tar)."""

    path: str
    required: bool
    note: str
    format: FormatContract | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Return a JSON-safe view; the nested ``format`` is present only when the member has it."""
        data: dict[str, JsonValue] = {
            "path": self.path,
            "required": self.required,
            "note": self.note,
        }
        if self.format is not None:
            data["format"] = self.format.to_json()
        return data


@dataclass(frozen=True, slots=True)
class ArtifactContract:
    """The full upload contract for one externally uploaded build artifact (#769, ADR-0234 §5)."""

    name: str
    requirement: Literal["required", "optional", "conditional"]
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


# The provider-neutral external-build upload contract, keyed by artifact name (ADR-0234 §5). The
# byte details (magic, layout member paths, the effective_config cap) are taken from this module's
# own validator constants, so the advertised contract cannot drift from what the validator enforces.
EXTERNAL_BUILD_CONTRACTS: Mapping[str, ArtifactContract] = {
    "kernel": ArtifactContract(
        name="kernel",
        requirement="required",
        summary=(
            "Combined kernel+modules tar (gzip): boot/vmlinuz (the bzImage, NOT the vmlinux ELF) "
            "plus lib/modules/<release>/. One artifact for both; there is no separate 'modules' "
            "upload."
        ),
        format=FormatContract(
            container="gzip tar",
            magic=(MagicPin(offset=0, hex=_GZIP_MAGIC.hex()),),
        ),
        layout=(
            LayoutMember(
                path=_KERNEL_BOOT_MEMBER,
                required=True,
                note="The bzImage (arch/x86/boot/bzImage), renamed to boot/vmlinuz in the tar.",
                format=FormatContract(
                    container="bzImage",
                    magic=(MagicPin(offset=_BZIMAGE_MAGIC_OFFSET, hex=_BZIMAGE_MAGIC.hex()),),
                ),
            ),
            LayoutMember(
                path=_MODULES_MEMBER_PREFIX,
                required=True,
                note=(
                    "The `make modules_install` tree: one or more lib/modules/<release>/ dirs. "
                    "Exclude the `build` and `source` back-reference symlinks."
                ),
            ),
        ),
        notes=(
            "Must be gzip specifically; a plain .tar, .tar.xz, or .tar.zst is rejected.",
            "List boot/vmlinuz before lib/modules: validation scans at most the first 128 MiB of "
            "decompressed output (a gzip-bomb guard), so the lib/modules header must be within it.",
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
        format=FormatContract(container="initramfs image"),
    ),
    "effective_config": ArtifactContract(
        name="effective_config",
        requirement="conditional",
        summary="The kernel .config used for the build.",
        format=FormatContract(
            container="kernel .config (text)",
            max_bytes=EFFECTIVE_CONFIG_MAX_BYTES,
        ),
        notes=(
            "Required when the Run's build profile carries profile_requirements; validated against "
            "that profile's required Kconfig symbols — the config install/boot expect to match.",
        ),
    ),
}


class ValidatorStore(HeadStore, Protocol):
    """Object-store operations needed by external build validation."""

    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...


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
    raise CategorizedError(
        "vmlinux carries no GNU build-id note",
        category=ErrorCategory.BUILD_FAILURE,
    )


def validate_external_artifacts(
    store: ValidatorStore,
    *,
    manifest: Sequence[ManifestEntry],
    keys: Mapping[str, str],
    declared_build_id: str | None,
    profile_requirements: ConfigRequirements | None = None,
) -> ValidatedUpload:
    """Validate uploaded build artifacts; return the ``BuildOutput`` plus object heads."""
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
        heads[name] = _validate_one_artifact(store, name, entry, key)
    if profile_requirements is not None:
        _validate_effective_config(
            store,
            keys=keys,
            heads=heads,
            profile_requirements=profile_requirements,
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
    return ValidatedUpload(output=output, heads=heads)


def _validate_effective_config(
    store: ValidatorStore,
    *,
    keys: Mapping[str, str],
    heads: Mapping[str, HeadResult],
    profile_requirements: ConfigRequirements,
) -> None:
    key = keys.get("effective_config")
    head = heads.get("effective_config")
    if key is None or head is None:
        raise CategorizedError(
            "external build profile requirements need an effective_config artifact",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if head.size_bytes > EFFECTIVE_CONFIG_MAX_BYTES:
        raise CategorizedError(
            "effective_config exceeds the readable size cap",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "name": "effective_config",
                "size_bytes": head.size_bytes,
                "max_size_bytes": EFFECTIVE_CONFIG_MAX_BYTES,
            },
        )
    data = store.get_range(key, start=0, length=head.size_bytes)
    validate_config_requirements(data.decode("utf-8", errors="replace"), profile_requirements)


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
        e_shstrndx = struct.unpack_from("<H", header, 0x3E)[0]
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
        shstr = _read_section(store, key, sht, e_shentsize, e_shstrndx, max_size=max_size)
        return _find_build_id_note(store, key, sht, shstr, e_shentsize, e_shnum, max_size=max_size)
    except (struct.error, ValueError, IndexError) as exc:
        raise _build_failure("vmlinux ELF is structurally malformed") from exc


def _validate_one_artifact(
    store: ValidatorStore, name: str, entry: ManifestEntry, key: str
) -> HeadResult:
    head = store.head(key)
    if head is None:
        raise CategorizedError(
            f"declared artifact {name!r} was never uploaded",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": name},
        )
    if entry.chunks is None:
        if head.size_bytes != entry.size_bytes or head.checksum_sha256 != entry.sha256:
            raise _build_failure("uploaded artifact disagrees with its manifest", name=name)
    elif head.size_bytes != entry.size_bytes:
        # The reassembled multipart object exposes only a composite checksum, so the
        # whole-object SHA-256 is not comparable here; the per-chunk pins (verify_chunks)
        # already bound every byte. Only the total size is checked on the final object.
        raise _build_failure("reassembled artifact size disagrees with its manifest", name=name)
    _check_artifact_content(store, name, key, head.size_bytes)
    return head


def _check_artifact_content(store: ValidatorStore, name: str, key: str, size_bytes: int) -> None:
    if name == "vmlinux":
        if store.get_range(key, start=0, length=4) != _ELF_MAGIC:
            raise _build_failure("vmlinux is not an ELF file", name=name)
    elif name == "kernel":
        _check_kernel_combined_tar(store, key, name, size_bytes=size_bytes)


def _check_kernel_combined_tar(
    store: ValidatorStore, key: str, name: str, *, size_bytes: int
) -> None:
    """Validate the external `kernel` upload is a combined kernel+modules tar (ADR-0234 §2).

    The artifact must be a gzip stream whose tar holds ``boot/vmlinuz`` (itself a bzImage) and at
    least one ``lib/modules/<ver>/`` member. The scan decompresses at most
    :data:`_KERNEL_TAR_SCAN_MAX_BYTES` so a gzip bomb cannot make this read unbounded; if both
    members are not seen within that bound the upload is rejected.
    """
    if store.get_range(key, start=0, length=2) != _GZIP_MAGIC:
        raise _build_failure("kernel artifact is not a gzip-compressed combined tar", name=name)
    data = _decompress_bounded(
        store, key, total_size=size_bytes, max_out=_KERNEL_TAR_SCAN_MAX_BYTES
    )
    _verify_combined_tar_shape(data, name)


def _decompress_bounded(store: ValidatorStore, key: str, *, total_size: int, max_out: int) -> bytes:
    """Gunzip ``key`` via sequential ranged reads, stopping at ``max_out`` decompressed bytes."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)  # 16 + MAX_WBITS selects gzip framing
    out = bytearray()
    offset = 0
    while offset < total_size and len(out) < max_out:
        length = min(_RANGE_CHUNK_BYTES, total_size - offset)
        chunk = store.get_range(key, start=offset, length=length)
        if not chunk:
            break
        offset += len(chunk)
        out += decompressor.decompress(chunk, max_out - len(out))
        if decompressor.eof:
            break
    return bytes(out)


def _verify_combined_tar_shape(data: bytes, name: str) -> None:
    boot_ok = False
    modules_ok = False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                path = _normalized_member_name(member.name)
                if path == _KERNEL_BOOT_MEMBER and member.isfile():
                    boot_ok = _member_is_bzimage(archive, member)
                elif path.startswith(_MODULES_MEMBER_PREFIX):
                    modules_ok = True
                if boot_ok and modules_ok:
                    break
    except tarfile.TarError as exc:
        # An open failure (not a tar at all) is fatal; a truncation mid-iteration is the expected
        # outcome when the decompress bound cut the tail — fall through to the member checks so a
        # gzip bomb surfaces as a precise "no lib/modules within the scan bound".
        if not (boot_ok or modules_ok):
            raise _build_failure("kernel artifact is not a readable tar", name=name) from exc
    if not boot_ok:
        raise _build_failure("kernel combined tar has no boot/vmlinuz bzImage member", name=name)
    if not modules_ok:
        raise _build_failure(
            "kernel combined tar has no lib/modules member within the scan bound", name=name
        )


def _member_is_bzimage(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bool:
    extracted = archive.extractfile(member)
    if extracted is None:
        return False
    head = extracted.read(_BZIMAGE_MAGIC_OFFSET + 4)
    return head[_BZIMAGE_MAGIC_OFFSET : _BZIMAGE_MAGIC_OFFSET + 4] == _BZIMAGE_MAGIC


def _normalized_member_name(name: str) -> str:
    if name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def _find_build_id_note(
    store: ValidatorStore,
    key: str,
    sht: bytes,
    shstr: bytes,
    e_shentsize: int,
    e_shnum: int,
    *,
    max_size: int,
) -> str:
    for i in range(e_shnum):
        off = i * e_shentsize
        sh_name = struct.unpack_from("<I", sht, off)[0]
        sh_type = struct.unpack_from("<I", sht, off + 4)[0]
        if sh_type != _SHT_NOTE:
            continue
        _section_name_end = shstr.index(b"\x00", sh_name)
        notes = _read_section(store, key, sht, e_shentsize, i, max_size=max_size)
        try:
            return parse_gnu_build_id(notes)
        except CategorizedError:
            continue
    raise _build_failure("vmlinux carries no GNU build-id note")


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
