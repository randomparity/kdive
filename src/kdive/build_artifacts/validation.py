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
# and the expected_uploads advertisement read, so they cannot drift. x86_64 is the bzImage HdrS
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
            "boot (root=/dev/vda ext4 on virtio-blk); see artifacts.feature_config_requirements.",
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
        heads[name] = _validate_one_artifact(store, name, entry, key, boot_format)

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
    store: ValidatorStore, name: str, entry: ManifestEntry, key: str, boot_format: FormatContract
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
    _check_artifact_content(store, name, key, head.size_bytes, boot_format)
    return head


def _check_artifact_content(
    store: ValidatorStore, name: str, key: str, size_bytes: int, boot_format: FormatContract
) -> None:
    if name == "vmlinux":
        if store.get_range(key, start=0, length=4) != _ELF_MAGIC:
            raise _build_failure("vmlinux is not an ELF file", name=name)
    elif name == "kernel":
        _check_kernel_combined_tar(store, key, name, size_bytes=size_bytes, boot_format=boot_format)


def _check_kernel_combined_tar(
    store: ValidatorStore, key: str, name: str, *, size_bytes: int, boot_format: FormatContract
) -> None:
    """Validate the external `kernel` upload is a combined kernel+modules tar (ADR-0234 §2).

    The artifact must be a gzip stream whose tar holds ``boot/vmlinuz`` (matching ``boot_format``
    for the declared arch — a bzImage for x86_64, an ELF kernel for ppc64le) and at least one
    ``lib/modules/<ver>/`` member. The scan decompresses at most
    :data:`_KERNEL_TAR_SCAN_MAX_BYTES` so a gzip bomb cannot make this read unbounded; if both
    members are not seen within that bound the upload is rejected.
    """
    if store.get_range(key, start=0, length=2) != _GZIP_MAGIC:
        raise _build_failure("kernel artifact is not a gzip-compressed combined tar", name=name)
    data, cap_reached = _decompress_bounded(
        store, key, total_size=size_bytes, max_out=_KERNEL_TAR_SCAN_MAX_BYTES
    )
    _verify_combined_tar_shape(data, name, boot_format, cap_reached=cap_reached)


def _decompress_bounded(
    store: ValidatorStore, key: str, *, total_size: int, max_out: int
) -> tuple[bytes, bool]:
    """Gunzip ``key`` via sequential ranged reads, stopping at ``max_out`` decompressed bytes.

    Returns the decompressed prefix and ``cap_reached`` (``True`` when the ``max_out`` bound cut
    the stream short rather than reaching a clean gzip EOF), which lets the caller tell an
    oversized boot member from a genuinely short tar.
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
        out += decompressor.decompress(chunk, max_out - len(out))
        if decompressor.eof:
            break
    return bytes(out), len(out) >= max_out


def _verify_combined_tar_shape(
    data: bytes, name: str, boot_format: FormatContract, *, cap_reached: bool
) -> None:
    boot_ok = False
    modules_ok = False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                path = _normalized_member_name(member.name)
                if path == _KERNEL_BOOT_MEMBER and member.isfile():
                    boot_ok = _member_matches_format(archive, member, boot_format)
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
        raise _build_failure(
            f"kernel combined tar has no boot/vmlinuz {boot_format.container} member", name=name
        )
    if not modules_ok:
        if cap_reached:
            raise _build_failure(
                "kernel combined tar boot/vmlinuz exceeds the scan bound before any lib/modules "
                "member; strip the boot image or list lib/modules earlier",
                name=name,
            )
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
