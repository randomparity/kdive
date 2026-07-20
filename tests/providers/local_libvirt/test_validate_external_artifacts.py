"""External-artifact validation (ADR-0048 §5)."""

from __future__ import annotations

import io
import struct
import tarfile

import pytest

from kdive.artifacts.storage import HeadResult
from kdive.artifacts.uploads import ManifestEntry
from kdive.build_artifacts import validation
from kdive.build_artifacts.validation import (
    extract_build_id_ranged,
    validate_external_artifacts,
)
from kdive.domain.errors import CategorizedError, ErrorCategory

_BZIMAGE_BODY = b"\x00" * 0x202 + b"HdrS" + b"\x00" * 16  # bzImage magic at offset 0x202
_EM_PPC64 = 21
_EM_X86_64 = 62


def _boot_elf(*, e_machine: int = _EM_PPC64, pad: int = 0) -> bytes:
    """A minimal ELF64-LE boot member with the given ``e_machine`` at offset 0x12."""
    body = bytearray(0x40)
    body[0:4] = b"\x7fELF"
    body[4] = 2  # ELFCLASS64
    body[5] = 1  # ELFDATA2LSB
    struct.pack_into("<H", body, 0x12, e_machine)  # e_machine
    return bytes(body) + b"\x00" * pad


def _tar_add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _combined_kernel_tar(
    *,
    boot: bytes | None = _BZIMAGE_BODY,
    with_modules: bool = True,
    real_module: bool = True,
    version: str = "6.9.0",
) -> bytes:
    """A gzip combined tar: boot/vmlinuz (optional) + lib/modules/<ver>/ (optional).

    ``real_module`` controls whether the modules tree carries an actual ``*.ko`` file
    (the validator's requirement) or only a ``modules.dep`` with no kernel module — the
    shallow-prefix blind spot #1273 closes.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if boot is not None:
            _tar_add(tar, "boot/vmlinuz", boot)
        if with_modules:
            _tar_add(tar, f"lib/modules/{version}/modules.dep", b"")
            if real_module:
                _tar_add(tar, f"lib/modules/{version}/kernel/drivers/foo.ko", b"\x7fELFmod")
    return buf.getvalue()


_KERNEL_TAR = _combined_kernel_tar()


class _FakeStore:
    def __init__(self, blobs: dict[str, bytes], heads: dict[str, HeadResult]) -> None:
        self._blobs = blobs
        self._heads = heads
        self.range_calls: list[tuple[str, int, int]] = []

    def head(self, key: str) -> HeadResult | None:
        return self._heads.get(key)

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        self.range_calls.append((key, start, length))
        return self._blobs[key][start : start + length]


def _elf_with_build_id(
    build_id: bytes, *, note_section_name: bytes = b".note.gnu.build-id"
) -> bytes:
    """Minimal ELF64-LE blob carrying a GNU build-id SHT_NOTE section.

    Layout (offsets chosen so extract_build_id_ranged round-trips):
      [0:64]   ELF64 header
      then     note section bytes, shstrtab bytes, section header table
    """
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    # section-name string table: index 0 = "", then the two section names.
    shstrtab = b"\x00.shstrtab\x00" + note_section_name + b"\x00"
    name_shstrtab = shstrtab.index(b".shstrtab")
    name_note = shstrtab.index(note_section_name)

    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    # We'll lay out: header(64) | note | shstrtab | SHT
    note_off = 64
    shstr_off = note_off + len(note)
    sht_off = shstr_off + len(shstrtab)
    e_shentsize = 64
    e_shnum = 3  # null, .note.gnu.build-id, .shstrtab
    e_shstrndx = 2  # .shstrtab is section index 2
    struct.pack_into("<Q", header, 0x28, sht_off)  # e_shoff
    struct.pack_into("<H", header, 0x3A, e_shentsize)  # e_shentsize
    struct.pack_into("<H", header, 0x3C, e_shnum)  # e_shnum
    struct.pack_into("<H", header, 0x3E, e_shstrndx)  # e_shstrndx

    def section(sh_name: int, sh_type: int, sh_offset: int, sh_size: int) -> bytes:
        sh = bytearray(64)
        struct.pack_into("<I", sh, 0x00, sh_name)
        struct.pack_into("<I", sh, 0x04, sh_type)
        struct.pack_into("<Q", sh, 0x18, sh_offset)
        struct.pack_into("<Q", sh, 0x20, sh_size)
        return bytes(sh)

    sht = (
        section(0, 0, 0, 0)  # SHN_UNDEF
        + section(name_note, 7, note_off, len(note))  # SHT_NOTE
        + section(name_shstrtab, 3, shstr_off, len(shstrtab))  # SHT_STRTAB
    )
    return bytes(header) + note + shstrtab + sht


def test_missing_kernel_is_configuration_error() -> None:
    store = _FakeStore({}, {})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(store, manifest=[], keys={}, declared_build_id=None)
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_missing_object_is_configuration_error() -> None:
    store = _FakeStore({}, {})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "csum", 6)],
            keys={"kernel": "k"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_checksum_mismatch_is_build_failure() -> None:
    # #1338: a stored checksum that is present but DIFFERS keeps the generic "disagrees" message —
    # this is a genuine wrong-bytes mismatch, distinct from the absent-checksum bypass case below.
    store = _FakeStore(
        {"k": _KERNEL_TAR},
        {"k": HeadResult(size_bytes=len(_KERNEL_TAR), checksum_sha256="OTHER", etag="e")},
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "csum", len(_KERNEL_TAR))],
            keys={"kernel": "k"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    assert "disagrees with its manifest" in str(e.value)
    assert "no stored SHA-256 checksum" not in str(e.value)


def test_absent_stored_checksum_names_the_bypass_cause() -> None:
    # #1338 / ADR-0395: a single-PUT object with no stored SHA-256 (checksum_sha256 is None) was
    # not written through the presigned PUT — a direct put_object that skipped the signed
    # x-amz-checksum-sha256 header. The rejection must name that cause, distinct from a genuine
    # checksum mismatch, so the agent can tell "checksum absent (bypass)" from "checksum differs".
    store = _FakeStore(
        {"k": _KERNEL_TAR},
        {"k": HeadResult(size_bytes=len(_KERNEL_TAR), checksum_sha256=None, etag="e")},
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "csum", len(_KERNEL_TAR))],
            keys={"kernel": "k"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    message = str(e.value)
    assert "no stored SHA-256 checksum" in message
    assert "bypassed the presigned PUT" in message
    assert "x-amz-checksum-sha256" in message
    assert "disagrees with its manifest" not in message


def _validate_kernel_blob(blob: bytes, *, arch: str = "x86_64") -> None:
    """Wire a `kernel` blob through validate_external_artifacts to reach the content check."""
    store = _FakeStore({"k": blob}, {"k": HeadResult(len(blob), "csum", "e")})
    validate_external_artifacts(
        store,
        manifest=[ManifestEntry("kernel", "csum", len(blob))],
        keys={"kernel": "k"},
        declared_build_id=None,
        arch=arch,
    )


def test_non_gzip_kernel_is_build_failure() -> None:
    # A raw bzImage (the legacy local format) and any non-gzip blob are now rejected: the unified
    # `kernel` artifact must be a gzip-compressed combined tar (ADR-0234 §2).
    for blob in (b"\x00" * 0x300, _BZIMAGE_BODY):
        with pytest.raises(CategorizedError) as e:
            _validate_kernel_blob(blob)
        assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_gzip_non_tar_kernel_is_build_failure() -> None:
    import gzip

    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(gzip.compress(b"not a tar at all"))
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_kernel_tar_missing_boot_vmlinuz_is_build_failure() -> None:
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(_combined_kernel_tar(boot=None))
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    # Absent member: "has no ... member" (present-but-wrong uses a distinct message).
    assert "has no boot/vmlinuz" in str(e.value)


def test_kernel_tar_boot_not_bzimage_is_build_failure() -> None:
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(_combined_kernel_tar(boot=b"\x00" * 0x300))
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    # Member present but wrong format: names it as present, not missing.
    assert "boot/vmlinuz is present but is not" in str(e.value)


def test_kernel_tar_missing_lib_modules_is_build_failure() -> None:
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(_combined_kernel_tar(with_modules=False))
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    assert "lib/modules" in str(e.value)


def test_kernel_tar_scan_is_bounded_against_a_decompression_bomb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With the decompress bound set below the boot/vmlinuz payload, the lib/modules header is never
    # reached, so validation rejects rather than decompressing unbounded — the gzip-bomb guard.
    import kdive.build_artifacts.validation as validation_module

    monkeypatch.setattr(validation_module, "_KERNEL_TAR_SCAN_MAX_BYTES", 256)
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(_combined_kernel_tar(boot=b"\x00" * (4 * 1024)))
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_kernel_tar_modules_prefix_without_ko_is_build_failure() -> None:
    # A modules tree that has lib/modules/<ver>/modules.dep but no real *.ko file: the shallow
    # startswith("lib/modules/") match used to accept it (#1273); a real module is now required.
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(_combined_kernel_tar(real_module=False))
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    assert "lib/modules" in str(e.value)


def test_kernel_tar_truncated_tail_is_build_failure() -> None:
    # A .tar.gz whose gzip trailer (CRC32+ISIZE) was cut off: the deflate content still inflates
    # to the full tar (boot + a real .ko are present), so the pre-#1273 scan accepted it. The
    # stream never reaches a clean gzip EOF below the scan cap, so it is now rejected at upload.
    blob = _combined_kernel_tar()[:-8]
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(blob)
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    # Names the truncated-stream cause specifically (not the generic missing-modules message),
    # so a mutant that drops the gzip-completeness gate fails rather than mislabeling.
    assert "truncated" in str(e.value) and "gzip" in str(e.value)


def test_kernel_tar_corrupt_gzip_trailer_is_build_failure() -> None:
    # A .tar.gz whose prefix inflates cleanly but whose gzip trailer is corrupt (ISIZE byte
    # flipped): zlib raises when it consumes the bad trailer. Pre-#1273 that surfaced as an
    # uncategorized zlib.error; it must be a categorized BUILD_FAILURE.
    blob = bytearray(_combined_kernel_tar())
    blob[-1] ^= 0xFF  # corrupt the gzip ISIZE trailer -> zlib "incorrect length check"
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(bytes(blob))
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    assert "corrupt" in str(e.value) and "gzip" in str(e.value)


def test_happy_path_kernel_only_returns_build_output() -> None:
    store = _FakeStore({"k": _KERNEL_TAR}, {"k": HeadResult(len(_KERNEL_TAR), "csum", "e")})
    out = validate_external_artifacts(
        store,
        manifest=[ManifestEntry("kernel", "csum", len(_KERNEL_TAR))],
        keys={"kernel": "k"},
        declared_build_id=None,
    )
    assert (
        out.output.kernel_ref == "k"
        and out.output.debuginfo_ref == ""
        and out.output.build_id == ""
    )
    assert set(out.heads) == {"kernel"}


def test_build_id_mismatch_is_build_failure() -> None:
    blob = _elf_with_build_id(bytes.fromhex("dead"))
    store = _FakeStore(
        {"k": _KERNEL_TAR, "v": blob},
        {"k": HeadResult(len(_KERNEL_TAR), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[
                ManifestEntry("kernel", "ck", len(_KERNEL_TAR)),
                ManifestEntry("vmlinux", "cv", len(blob)),
            ],
            keys={"kernel": "k", "vmlinux": "v"},
            declared_build_id="beef",
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_vmlinux_without_declared_build_id_is_configuration_error() -> None:
    blob = _elf_with_build_id(bytes.fromhex("dead"))
    store = _FakeStore(
        {"k": _KERNEL_TAR, "v": blob},
        {"k": HeadResult(len(_KERNEL_TAR), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[
                ManifestEntry("kernel", "ck", len(_KERNEL_TAR)),
                ManifestEntry("vmlinux", "cv", len(blob)),
            ],
            keys={"kernel": "k", "vmlinux": "v"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_matching_build_id_passes_and_pairs_vmlinux() -> None:
    blob = _elf_with_build_id(bytes.fromhex("deadbeef"))
    store = _FakeStore(
        {"k": _KERNEL_TAR, "v": blob},
        {"k": HeadResult(len(_KERNEL_TAR), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    out = validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(_KERNEL_TAR)),
            ManifestEntry("vmlinux", "cv", len(blob)),
        ],
        keys={"kernel": "k", "vmlinux": "v"},
        declared_build_id="DEADBEEF",  # case-insensitive vs the lowercase-hex note
    )
    assert out.output.kernel_ref == "k" and out.output.debuginfo_ref == "v"
    assert out.output.build_id == "deadbeef"


def test_initrd_is_validated_and_returned_in_keys() -> None:
    store = _FakeStore(
        {"k": _KERNEL_TAR, "i": b"\x1f\x8b" + b"\x00" * 40},
        {"k": HeadResult(len(_KERNEL_TAR), "ck", "e"), "i": HeadResult(42, "ci", "e")},
    )
    out = validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(_KERNEL_TAR)),
            ManifestEntry("initrd", "ci", 42),
        ],
        keys={"kernel": "k", "initrd": "i"},
        declared_build_id=None,
    )
    assert out.output.kernel_ref == "k"
    assert set(out.heads) == {"kernel", "initrd"}


def test_effective_config_accepted_without_validation() -> None:
    # A .config the retired rootfs-mount gate would reject (no SQUASHFS/OVERLAY_FS) is accepted
    # verbatim: its bytes are never read and no Kconfig symbol is checked.
    config = b"# CONFIG_SQUASHFS is not set\n# CONFIG_OVERLAY_FS is not set\n"
    store = _FakeStore(
        {"k": _KERNEL_TAR, "c": config},
        {
            "k": HeadResult(len(_KERNEL_TAR), "ck", "e"),
            "c": HeadResult(len(config), "cc", "ec"),
        },
    )

    out = validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(_KERNEL_TAR)),
            ManifestEntry("effective_config", "cc", len(config)),
        ],
        keys={"kernel": "k", "effective_config": "c"},
        declared_build_id=None,
    )

    assert set(out.heads) == {"kernel", "effective_config"}
    # The config bytes were never range-read; only its head was taken.
    assert not any(call[0] == "c" for call in store.range_calls)


def test_vmlinux_without_upload_key_is_configuration_error() -> None:
    store = _FakeStore({"k": _KERNEL_TAR}, {"k": HeadResult(len(_KERNEL_TAR), "ck", "e")})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[
                ManifestEntry("kernel", "ck", len(_KERNEL_TAR)),
                ManifestEntry("vmlinux", "cv", 64),
            ],
            keys={"kernel": "k"},  # vmlinux declared but no upload key
            declared_build_id="deadbeef",
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def _validate_vmlinux_blob(blob: bytes) -> None:
    """Wire a vmlinux blob through validate_external_artifacts to reach the extractor."""
    store = _FakeStore(
        {"k": _KERNEL_TAR, "v": blob},
        {"k": HeadResult(len(_KERNEL_TAR), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(_KERNEL_TAR)),
            ManifestEntry("vmlinux", "cv", len(blob)),
        ],
        keys={"kernel": "k", "vmlinux": "v"},
        declared_build_id="deadbeef",
    )


def test_truncated_elf_header_is_build_failure() -> None:
    blob = b"\x7fELF\x02\x01" + b"\x00" * 2  # passes magic/class/endian, header < 64 bytes
    with pytest.raises(CategorizedError) as e:
        _validate_vmlinux_blob(blob)
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_extract_build_id_ignores_invalid_section_name_table_index() -> None:
    blob = bytearray(_elf_with_build_id(bytes.fromhex("deadbeef")))
    e_shnum = struct.unpack_from("<H", blob, 0x3C)[0]
    struct.pack_into("<H", blob, 0x3E, e_shnum + 5)  # e_shstrndx points past the SHT

    _validate_vmlinux_blob(bytes(blob))


def test_extract_build_id_ignores_note_section_name_offset() -> None:
    blob = bytearray(_elf_with_build_id(bytes.fromhex("deadbeef")))
    e_shoff = struct.unpack_from("<Q", blob, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", blob, 0x3A)[0]
    note_sh_name_off = e_shoff + 1 * e_shentsize  # section index 1 is the SHT_NOTE entry
    struct.pack_into("<I", blob, note_sh_name_off, 0xFFFF)  # sh_name far past the shstrtab

    _validate_vmlinux_blob(bytes(blob))


def test_extract_build_id_accepts_nonstandard_note_section_name() -> None:
    build_id = bytes.fromhex("0123456789abcdef")
    blob = _elf_with_build_id(build_id, note_section_name=b".notes")
    store = _FakeStore({"v": blob}, {})

    assert extract_build_id_ranged(store, "v", max_size=len(blob)) == build_id.hex()


@pytest.mark.parametrize(
    "exc",
    [
        CategorizedError(
            "note parser dependency failed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ),
        CategorizedError(
            "unexpected note parser build failure",
            category=ErrorCategory.BUILD_FAILURE,
        ),
    ],
)
def test_extract_build_id_reraises_unexpected_categorized_note_errors(
    exc: CategorizedError, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = _elf_with_build_id(bytes.fromhex("deadbeef"))
    store = _FakeStore({"v": blob}, {})

    def fail_parse(notes: bytes) -> str:
        del notes
        raise exc

    monkeypatch.setattr(validation, "parse_gnu_build_id", fail_parse)

    with pytest.raises(CategorizedError) as caught:
        extract_build_id_ranged(store, "v", max_size=len(blob))

    assert caught.value is exc


def test_extract_build_id_ranged_truncated_header_is_build_failure() -> None:
    blob = b"\x7fELF\x02\x01"
    store = _FakeStore({"v": blob}, {})
    with pytest.raises(CategorizedError) as e:
        extract_build_id_ranged(store, "v", max_size=len(blob))
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def _tamper_note_sh_size(blob: bytes, sh_size: int) -> bytes:
    """Return ``blob`` with the .note.gnu.build-id section's sh_size overwritten.

    Section index 1 in ``_elf_with_build_id`` is the SHT_NOTE entry; sh_size lives at
    offset 0x20 within its 64-byte SHT entry. Tampering the SHT (which trails the data)
    leaves ``len(blob)`` — hence the head's declared size — unchanged.
    """
    mutable = bytearray(blob)
    e_shoff = struct.unpack_from("<Q", mutable, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", mutable, 0x3A)[0]
    struct.pack_into("<Q", mutable, e_shoff + 1 * e_shentsize + 0x20, sh_size)
    return bytes(mutable)


def test_oversized_section_header_table_is_build_failure() -> None:
    # e_shentsize*e_shnum past the 16 MiB cap, but the header is within a large max_size so
    # the per-object guard passes — the absolute SHT cap must catch it before the get_range.
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    struct.pack_into("<Q", header, 0x28, 64)  # e_shoff
    struct.pack_into("<H", header, 0x3A, 512)  # e_shentsize
    struct.pack_into("<H", header, 0x3C, 0xFFFF)  # e_shnum -> 512*65535 == 32 MiB > 16 MiB
    struct.pack_into("<H", header, 0x3E, 0)  # e_shstrndx
    store = _FakeStore({"v": bytes(header)}, {})
    with pytest.raises(CategorizedError) as e:
        extract_build_id_ranged(store, "v", max_size=64 * 1024 * 1024)
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    # Tie the assertion to the SHT cap specifically: only that guard sets ``sht_bytes``.
    # Without it the empty fake-store SHT read would still raise BUILD_FAILURE via a
    # struct.error, so a bare category check would pass even with the guard removed.
    assert e.value.details.get("sht_bytes") == 512 * 0xFFFF


def test_oversized_section_size_is_build_failure() -> None:
    base = _elf_with_build_id(bytes.fromhex("deadbeef"))
    # Past the object size (max_size == len(blob)) but under the per-section cap.
    past_object = _tamper_note_sh_size(base, len(base) + 1)
    # Past the per-section cap (16 MiB).
    past_cap = _tamper_note_sh_size(base, 17 * 1024 * 1024)
    for blob in (past_object, past_cap):
        with pytest.raises(CategorizedError) as e:
            _validate_vmlinux_blob(blob)
        assert e.value.category is ErrorCategory.BUILD_FAILURE


# --- Arch-keyed boot-member payload (#1145, ADR-0343) -----------------------------------


def test_boot_member_formats_covers_supported_arches() -> None:
    # The profile-parse gate (SUPPORTED_ARCHES) and the payload-format gate
    # (BOOT_MEMBER_FORMATS) must agree, or a create-accepted arch could finalize-reject.
    from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES

    assert set(validation.BOOT_MEMBER_FORMATS) == SUPPORTED_ARCHES


def test_ppc64le_elf_boot_member_validates() -> None:
    # A combined tar whose boot/vmlinuz is a ppc64le ELF64-LE kernel validates under ppc64le.
    _validate_kernel_blob(_combined_kernel_tar(boot=_boot_elf()), arch="ppc64le")


def test_x86_bzimage_under_ppc64le_is_build_failure() -> None:
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(_combined_kernel_tar(boot=_BZIMAGE_BODY), arch="ppc64le")
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    assert "ppc64le" in str(e.value)


def test_elf_boot_member_under_x86_64_is_build_failure() -> None:
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(_combined_kernel_tar(boot=_boot_elf()), arch="x86_64")
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    assert "bzImage" in str(e.value)


def test_non_ppc64_elf_under_ppc64le_is_build_failure() -> None:
    # The e_machine pin: an x86_64 ELF64-LE begins with the same \x7fELF\x02\x01 prefix but
    # carries EM_X86_64 (62), not EM_PPC64 (21). The prefix alone would leak it in; the
    # e_machine check rejects it.
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(
            _combined_kernel_tar(boot=_boot_elf(e_machine=_EM_X86_64)), arch="ppc64le"
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    assert "ppc64le" in str(e.value)


def test_unknown_arch_to_validator_is_configuration_error() -> None:
    # Defensive fail-fast: the validator does not trust its caller (the profile-parse gate
    # already rejected an unknown arch upstream).
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(_KERNEL_TAR, arch="s390x")
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_small_boot_only_tar_reports_plain_missing_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A valid boot member with no lib/modules, well under the scan cap, keeps the plain message.
    import kdive.build_artifacts.validation as validation_module

    monkeypatch.setattr(validation_module, "_KERNEL_TAR_SCAN_MAX_BYTES", 1 * 1024 * 1024)
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(
            _combined_kernel_tar(boot=_boot_elf(), with_modules=False), arch="ppc64le"
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    assert "no lib/modules member within the scan bound" in str(e.value)
    assert "exceeds the scan bound" not in str(e.value)


def test_oversized_boot_member_reports_cap_reached_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Boot member fully readable (boot_ok) but the scan cap lands at the lib/modules header, so
    # modules is never seen: the message names the oversized-boot-member cause, not a bare
    # "no lib/modules". The boot member occupies 512 (tar header) + 2048 (content) = 2560 bytes,
    # so a 2560-byte cap reaches exactly its end and stops before the lib/modules header.
    import kdive.build_artifacts.validation as validation_module

    monkeypatch.setattr(validation_module, "_KERNEL_TAR_SCAN_MAX_BYTES", 2560)
    blob = _combined_kernel_tar(boot=_boot_elf(pad=2048 - 0x40), with_modules=True)
    with pytest.raises(CategorizedError) as e:
        _validate_kernel_blob(blob, arch="ppc64le")
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    assert "exceeds the scan bound" in str(e.value)


def test_ppc64le_vmlinux_build_id_pairs() -> None:
    # The optional vmlinux build-id path is ELF64-LE and already fits ppc64le: a ppc64le-flavored
    # debug ELF (EM_PPC64) with a GNU build-id note resolves and pairs.
    blob = bytearray(_elf_with_build_id(bytes.fromhex("deadbeef")))
    struct.pack_into("<H", blob, 0x12, _EM_PPC64)  # mark it ppc64le
    kernel = _combined_kernel_tar(boot=_boot_elf())
    store = _FakeStore(
        {"k": kernel, "v": bytes(blob)},
        {"k": HeadResult(len(kernel), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    out = validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(kernel)),
            ManifestEntry("vmlinux", "cv", len(blob)),
        ],
        keys={"kernel": "k", "vmlinux": "v"},
        declared_build_id="deadbeef",
        arch="ppc64le",
    )
    assert out.output.debuginfo_ref == "v" and out.output.build_id == "deadbeef"


# --- Chunked artifacts (ADR-0104 §4) ----------------------------------------------------

from kdive.artifacts.chunks import verify_chunks  # noqa: E402
from kdive.artifacts.uploads import ChunkEntry  # noqa: E402

_PREFIX = "local/runs/rid/"


def _chunked_entry() -> ManifestEntry:
    return ManifestEntry("vmlinux", "whole", 10, chunks=(ChunkEntry("c0", 6), ChunkEntry("c1", 4)))


def test_verify_chunks_passes_when_each_chunk_matches() -> None:
    store = _FakeStore(
        {},
        {
            f"{_PREFIX}vmlinux.part0001": HeadResult(6, "c0", "e"),
            f"{_PREFIX}vmlinux.part0002": HeadResult(4, "c1", "e"),
        },
    )
    verify_chunks(store, _PREFIX, _chunked_entry())  # does not raise


def test_verify_chunks_missing_chunk_is_configuration_error() -> None:
    store = _FakeStore({}, {f"{_PREFIX}vmlinux.part0001": HeadResult(6, "c0", "e")})
    with pytest.raises(CategorizedError) as e:
        verify_chunks(store, _PREFIX, _chunked_entry())
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_verify_chunks_non_chunked_entry_is_configuration_error() -> None:
    store = _FakeStore({}, {})
    entry = ManifestEntry("vmlinux", "whole", 10)

    with pytest.raises(CategorizedError) as exc:
        verify_chunks(store, _PREFIX, entry)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"name": "vmlinux"}


def test_verify_chunks_checksum_mismatch_is_build_failure() -> None:
    store = _FakeStore(
        {},
        {
            f"{_PREFIX}vmlinux.part0001": HeadResult(6, "WRONG", "e"),
            f"{_PREFIX}vmlinux.part0002": HeadResult(4, "c1", "e"),
        },
    )
    with pytest.raises(CategorizedError) as e:
        verify_chunks(store, _PREFIX, _chunked_entry())
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_chunked_entry_skips_whole_object_checksum_on_final_object() -> None:
    # The reassembled final object exposes a composite/None checksum; validation must accept
    # it on size + content alone for a chunked entry (the per-chunk checks happened earlier).
    final = _KERNEL_TAR
    entry = ManifestEntry("kernel", "whole", len(final), chunks=(ChunkEntry("c0", len(final)),))
    store = _FakeStore({"k": final}, {"k": HeadResult(len(final), None, "e")})
    out = validate_external_artifacts(
        store, manifest=[entry], keys={"kernel": "k"}, declared_build_id=None
    )
    assert out.output.kernel_ref == "k"


def test_chunked_entry_final_size_mismatch_is_build_failure() -> None:
    final = _KERNEL_TAR
    entry = ManifestEntry("kernel", "whole", 9999, chunks=(ChunkEntry("c0", 9999),))
    store = _FakeStore({"k": final}, {"k": HeadResult(len(final), None, "e")})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store, manifest=[entry], keys={"kernel": "k"}, declared_build_id=None
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE
