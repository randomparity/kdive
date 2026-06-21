"""Unit tests for ``kdive.build_artifacts.validation``.

Covers ``patch_target_paths`` — the unified-diff path parser used to verify ``git apply``
actually changed the build tree (issue #227) — plus the provider-neutral external-artifact
validation surface (ELF build-id extraction, magic/manifest checks, chunk verification).
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kdive.artifacts.storage import HeadResult
from kdive.artifacts.uploads import ChunkEntry, ManifestEntry
from kdive.build_artifacts.validation import (
    extract_build_id_ranged,
    parse_gnu_build_id,
    patch_target_paths,
    snapshot_file_bytes,
    validate_external_artifacts,
    verify_chunks,
)
from kdive.components.requirements import ConfigRequirements
from kdive.domain.errors import CategorizedError, ErrorCategory

_MODIFY = "--- a/fs/dcache.c\n+++ b/fs/dcache.c\n@@ -1,2 +1,2 @@\n line1\n-line2\n+line2-patched\n"


def test_parses_modified_file_with_p1_strip() -> None:
    assert patch_target_paths(_MODIFY, strip=1) == {Path("fs/dcache.c")}


def test_new_file_ignores_dev_null_source() -> None:
    patch = "--- /dev/null\n+++ b/init/new.c\n@@ -0,0 +1 @@\n+hello\n"
    assert patch_target_paths(patch, strip=1) == {Path("init/new.c")}


def test_deleted_file_ignores_dev_null_target() -> None:
    patch = "--- a/init/old.c\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    assert patch_target_paths(patch, strip=1) == {Path("init/old.c")}


def test_multiple_files() -> None:
    patch = _MODIFY + "--- a/kernel/sched.c\n+++ b/kernel/sched.c\n@@ -1 +1 @@\n-a\n+b\n"
    assert patch_target_paths(patch, strip=1) == {
        Path("fs/dcache.c"),
        Path("kernel/sched.c"),
    }


def test_strips_trailing_timestamp_after_tab() -> None:
    patch = "--- a/fs/dcache.c\t2026-06-10 00:00:00\n+++ b/fs/dcache.c\t2026-06-10 00:00:01\n"
    assert patch_target_paths(patch, strip=1) == {Path("fs/dcache.c")}


def test_path_shallower_than_strip_is_dropped() -> None:
    # "+++ toplevel" has only one component; -p1 strips it, leaving nothing to target.
    assert patch_target_paths("--- toplevel\n+++ toplevel\n", strip=1) == set()


def test_empty_patch_has_no_targets() -> None:
    assert patch_target_paths("", strip=1) == set()


def test_git_quoted_path_is_skipped() -> None:
    # git c-quotes paths with special/non-ASCII bytes; they can't be reliably -p stripped,
    # so they are excluded (the caller's git-apply stderr check covers correctness instead).
    patch = '--- "a/fs/\\303\\251.c"\n+++ "b/fs/\\303\\251.c"\n'
    assert patch_target_paths(patch, strip=1) == set()


def test_default_strip_is_one() -> None:
    # The default strip drops exactly one leading component (the git a//b/ prefix), so calling
    # without an explicit strip must behave identically to strip=1.
    assert patch_target_paths(_MODIFY) == {Path("fs/dcache.c")}


def test_path_with_embedded_space_is_kept_whole() -> None:
    # The path runs up to the tab (or end of line), not the first space: only a literal tab
    # separates the path from a trailing timestamp, so a space inside the path is preserved.
    patch = "--- a/fs/my file.c\n+++ b/fs/my file.c\n"
    assert patch_target_paths(patch, strip=1) == {Path("fs/my file.c")}


def test_shallow_path_does_not_stop_later_files() -> None:
    # A path shallower than the strip count is skipped, but parsing must CONTINUE to the next
    # header — a `break` here would drop every file declared after a top-level entry.
    patch = "--- toplevel\n+++ toplevel\n--- a/fs/dcache.c\n+++ b/fs/dcache.c\n"
    assert patch_target_paths(patch, strip=1) == {Path("fs/dcache.c")}


# ---- external-artifact validation -----------------------------------------------------

_BZIMAGE_MAGIC_OFFSET = 0x202


class _FakeStore:
    """In-memory object store exposing the head + ranged-read surface validation needs."""

    def __init__(self, blobs: dict[str, bytes], heads: dict[str, HeadResult]) -> None:
        self._blobs = blobs
        self._heads = heads

    def head(self, key: str) -> HeadResult | None:
        return self._heads.get(key)

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        return self._blobs[key][start : start + length]


def _head(blob: bytes, checksum: str = "sha-x") -> HeadResult:
    return HeadResult(size_bytes=len(blob), checksum_sha256=checksum, etag="e")


def _bzimage() -> bytes:
    return b"\x00" * _BZIMAGE_MAGIC_OFFSET + b"HdrS" + b"\x00" * 16


def _elf_with_build_id(build_id: bytes) -> bytes:
    """Minimal ELF64-LE blob carrying one .note.gnu.build-id section.

    Layout: header(64) | note | shstrtab | section-header-table, with offsets chosen so
    extract_build_id_ranged round-trips the build id.
    """
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    shstrtab = b"\x00.shstrtab\x00.note.gnu.build-id\x00"
    name_shstrtab = shstrtab.index(b".shstrtab")
    name_note = shstrtab.index(b".note.gnu.build-id")

    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    note_off = 64
    shstr_off = note_off + len(note)
    sht_off = shstr_off + len(shstrtab)
    e_shentsize = 64
    e_shnum = 3
    e_shstrndx = 2
    struct.pack_into("<Q", header, 0x28, sht_off)
    struct.pack_into("<H", header, 0x3A, e_shentsize)
    struct.pack_into("<H", header, 0x3C, e_shnum)
    struct.pack_into("<H", header, 0x3E, e_shstrndx)

    def section(sh_name: int, sh_type: int, sh_offset: int, sh_size: int) -> bytes:
        sh = bytearray(64)
        struct.pack_into("<I", sh, 0x00, sh_name)
        struct.pack_into("<I", sh, 0x04, sh_type)
        struct.pack_into("<Q", sh, 0x18, sh_offset)
        struct.pack_into("<Q", sh, 0x20, sh_size)
        return bytes(sh)

    sht = (
        section(0, 0, 0, 0)
        + section(name_note, 7, note_off, len(note))
        + section(name_shstrtab, 3, shstr_off, len(shstrtab))
    )
    return bytes(header) + note + shstrtab + sht


def test_snapshot_file_bytes_reads_existing_and_none_for_missing(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"abc")
    assert snapshot_file_bytes(target) == b"abc"
    assert snapshot_file_bytes(tmp_path / "absent.bin") is None


def test_parse_gnu_build_id_roundtrips_a_gnu_note() -> None:
    build_id = bytes.fromhex("deadbeefcafe")
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    assert parse_gnu_build_id(note) == "deadbeefcafe"


def test_parse_gnu_build_id_with_no_note_raises_build_failure() -> None:
    with pytest.raises(CategorizedError) as exc:
        parse_gnu_build_id(b"")
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "vmlinux carries no GNU build-id note"


def test_parse_gnu_build_id_skips_a_non_gnu_note_then_finds_the_gnu_note() -> None:
    # A leading non-GNU note must be stepped over (name/desc padding to 4 bytes) so the GNU
    # note that follows is still found — guards the alignment arithmetic and loop advance.
    other_desc = b"\x01\x02\x03"  # descsz 3 -> padded to 4
    other = struct.pack("<III", 4, len(other_desc), 1) + b"GNU\x00" + other_desc + b"\x00"
    build_id = bytes.fromhex("abcdef01")
    gnu = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    assert parse_gnu_build_id(other + gnu) == "abcdef01"


def test_parse_gnu_build_id_skips_a_type3_note_whose_name_is_not_gnu() -> None:
    # A note with the right note_type (3 == NT_GNU_BUILD_ID) but a NON-"GNU" name (b"FOO") must be
    # skipped, not accepted: only `note_type == 3 AND name == b"GNU"` returns. Isolates the
    # `name == b"GNU"` conjunct — a mutant dropping it would return the FOO descriptor's hex.
    foo_desc = bytes.fromhex("11223344")
    foo = struct.pack("<III", 4, len(foo_desc), 3) + b"FOO\x00" + foo_desc
    build_id = bytes.fromhex("abcdef01")
    gnu = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    assert parse_gnu_build_id(foo + gnu) == "abcdef01"


def test_extract_build_id_ranged_returns_the_note_build_id() -> None:
    blob = _elf_with_build_id(bytes.fromhex("0011223344"))
    store = _FakeStore({"vmlinux": blob}, {})
    assert extract_build_id_ranged(store, "vmlinux", max_size=len(blob)) == "0011223344"


def test_extract_build_id_ranged_truncated_header_is_build_failure() -> None:
    store = _FakeStore({"vmlinux": b"\x7fELF" + b"\x00" * 10}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=14)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "vmlinux ELF header is truncated"


def test_extract_build_id_ranged_rejects_non_elf_magic() -> None:
    store = _FakeStore({"vmlinux": b"\x00" * 64}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=64)
    assert str(exc.value) == "vmlinux is not a 64-bit little-endian ELF"


def test_extract_build_id_ranged_rejects_32bit_class() -> None:
    # header[4] (EI_CLASS) must be 2 (ELFCLASS64); a 32-bit class is rejected even with valid
    # magic and little-endian data — guards the `and`/`or` join of the three header checks.
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 1  # ELFCLASS32
    header[5] = 1
    store = _FakeStore({"vmlinux": bytes(header)}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=64)
    assert str(exc.value) == "vmlinux is not a 64-bit little-endian ELF"


def test_extract_build_id_ranged_rejects_big_endian_data() -> None:
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 2  # ELFDATA2MSB
    store = _FakeStore({"vmlinux": bytes(header)}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=64)
    assert str(exc.value) == "vmlinux is not a 64-bit little-endian ELF"


def test_extract_build_id_ranged_no_section_header_table_is_build_failure() -> None:
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    # e_shoff stays 0 -> "no usable section header table"
    store = _FakeStore({"vmlinux": bytes(header)}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=64)
    assert str(exc.value) == "vmlinux has no usable section header table"


def test_extract_build_id_ranged_sht_past_object_size_is_build_failure() -> None:
    # The section header table is declared but extends past max_size: reported before any
    # out-of-range read is attempted.
    blob = _elf_with_build_id(bytes.fromhex("aa"))
    store = _FakeStore({"vmlinux": blob}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=len(blob) - 1)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "vmlinux section header table extends past the object size"


def _kernel_only(checksum: str = "kc") -> tuple[_FakeStore, list[ManifestEntry], dict[str, str]]:
    blob = _bzimage()
    store = _FakeStore({"k": blob}, {"k": _head(blob, checksum)})
    manifest = [ManifestEntry(name="kernel", sha256=checksum, size_bytes=len(blob))]
    return store, manifest, {"kernel": "k"}


def test_validate_external_artifacts_kernel_only_happy_path() -> None:
    store, manifest, keys = _kernel_only()
    result = validate_external_artifacts(
        store, manifest=manifest, keys=keys, declared_build_id=None
    )
    assert result.output.kernel_ref == "k"
    assert result.output.build_id == ""
    assert set(result.heads) == {"kernel"}


def test_validate_external_artifacts_missing_kernel_is_configuration_error() -> None:
    store = _FakeStore({}, {})
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(store, manifest=[], keys={}, declared_build_id=None)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "external build is missing the required kernel artifact"


def test_validate_external_artifacts_size_mismatch_is_build_failure() -> None:
    store, manifest, keys = _kernel_only()
    manifest = [ManifestEntry(name="kernel", sha256="kc", size_bytes=999999)]
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(store, manifest=manifest, keys=keys, declared_build_id=None)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "uploaded artifact disagrees with its manifest"
    assert exc.value.details == {"name": "kernel"}


def test_validate_external_artifacts_bad_kernel_magic_is_build_failure() -> None:
    blob = b"\x00" * (_BZIMAGE_MAGIC_OFFSET + 8)  # no HdrS at the bzImage offset
    store = _FakeStore({"k": blob}, {"k": _head(blob, "kc")})
    manifest = [ManifestEntry(name="kernel", sha256="kc", size_bytes=len(blob))]
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store, manifest=manifest, keys={"kernel": "k"}, declared_build_id=None
        )
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "kernel is not a bzImage"
    assert exc.value.details == {"name": "kernel"}


def test_validate_external_artifacts_uploaded_object_missing_is_configuration_error() -> None:
    manifest = [ManifestEntry(name="kernel", sha256="kc", size_bytes=4)]
    store = _FakeStore({}, {})  # head returns None
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store, manifest=manifest, keys={"kernel": "k"}, declared_build_id=None
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "declared artifact 'kernel' was never uploaded"
    assert exc.value.details == {"name": "kernel"}


def test_validate_external_artifacts_vmlinux_requires_declared_build_id() -> None:
    kblob = _bzimage()
    vblob = _elf_with_build_id(bytes.fromhex("00"))
    store = _FakeStore(
        {"k": kblob, "v": vblob},
        {"k": _head(kblob, "kc"), "v": _head(vblob, "vc")},
    )
    manifest = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob)),
        ManifestEntry(name="vmlinux", sha256="vc", size_bytes=len(vblob)),
    ]
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store, manifest=manifest, keys={"kernel": "k", "vmlinux": "v"}, declared_build_id=None
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "a vmlinux upload requires a declared build_id"


def test_validate_external_artifacts_build_id_mismatch_is_build_failure() -> None:
    kblob = _bzimage()
    vblob = _elf_with_build_id(bytes.fromhex("0011"))
    store = _FakeStore(
        {"k": kblob, "v": vblob},
        {"k": _head(kblob, "kc"), "v": _head(vblob, "vc")},
    )
    manifest = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob)),
        ManifestEntry(name="vmlinux", sha256="vc", size_bytes=len(vblob)),
    ]
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store,
            manifest=manifest,
            keys={"kernel": "k", "vmlinux": "v"},
            declared_build_id="ffff",
        )
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "declared build_id does not match the uploaded vmlinux"


def test_validate_external_artifacts_build_id_match_is_case_insensitive() -> None:
    kblob = _bzimage()
    vblob = _elf_with_build_id(bytes.fromhex("00aa11"))
    store = _FakeStore(
        {"k": kblob, "v": vblob},
        {"k": _head(kblob, "kc"), "v": _head(vblob, "vc")},
    )
    manifest = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob)),
        ManifestEntry(name="vmlinux", sha256="vc", size_bytes=len(vblob)),
    ]
    result = validate_external_artifacts(
        store,
        manifest=manifest,
        keys={"kernel": "k", "vmlinux": "v"},
        declared_build_id="00AA11",
    )
    assert result.output.build_id == "00aa11"
    assert result.output.debuginfo_ref == "v"


def test_validate_external_artifacts_missing_upload_key_is_configuration_error() -> None:
    blob = _bzimage()
    store = _FakeStore({"k": blob}, {"k": _head(blob, "kc")})
    manifest = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(blob)),
        ManifestEntry(name="initrd", sha256="ic", size_bytes=4),
    ]
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store, manifest=manifest, keys={"kernel": "k"}, declared_build_id=None
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "declared artifact 'initrd' has no upload key"
    assert exc.value.details == {"name": "initrd"}


def test_effective_config_required_when_profile_requirements_selected() -> None:
    store, manifest, keys = _kernel_only()
    reqs = ConfigRequirements(required={"CONFIG_FOO": "y"})
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store,
            manifest=manifest,
            keys=keys,
            declared_build_id=None,
            profile_requirements=reqs,
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "external build profile requirements need an effective_config artifact"


def test_effective_config_satisfies_requirements_passes() -> None:
    kblob = _bzimage()
    cfg = b"CONFIG_FOO=y\n"
    store = _FakeStore(
        {"k": kblob, "c": cfg},
        {"k": _head(kblob, "kc"), "c": _head(cfg, "cc")},
    )
    manifest = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob)),
        ManifestEntry(name="effective_config", sha256="cc", size_bytes=len(cfg)),
    ]
    reqs = ConfigRequirements(required={"CONFIG_FOO": "y"})
    result = validate_external_artifacts(
        store,
        manifest=manifest,
        keys={"kernel": "k", "effective_config": "c"},
        declared_build_id=None,
        profile_requirements=reqs,
    )
    assert "effective_config" in result.heads


def test_verify_chunks_passes_when_each_chunk_matches() -> None:
    chunks = (
        ChunkEntry(sha256="c1", size_bytes=10),
        ChunkEntry(sha256="c2", size_bytes=20),
    )
    entry = ManifestEntry(name="vmlinux", sha256="x", size_bytes=30, chunks=chunks)
    heads = {
        "p/vmlinux.part0001": HeadResult(size_bytes=10, checksum_sha256="c1", etag="e"),
        "p/vmlinux.part0002": HeadResult(size_bytes=20, checksum_sha256="c2", etag="e"),
    }
    store = _FakeStore({}, heads)
    verify_chunks(store, "p/", entry)  # does not raise


def test_verify_chunks_non_chunked_entry_is_configuration_error() -> None:
    entry = ManifestEntry(name="vmlinux", sha256="x", size_bytes=30)
    with pytest.raises(CategorizedError) as exc:
        verify_chunks(_FakeStore({}, {}), "p/", entry)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "artifact is not declared as chunked"
    assert exc.value.details == {"name": "vmlinux"}


def test_verify_chunks_missing_chunk_is_configuration_error() -> None:
    entry = ManifestEntry(
        name="vmlinux",
        sha256="x",
        size_bytes=10,
        chunks=(ChunkEntry(sha256="c1", size_bytes=10),),
    )
    with pytest.raises(CategorizedError) as exc:
        verify_chunks(_FakeStore({}, {}), "p/", entry)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "declared chunk 1 of 'vmlinux' was never uploaded"
    assert exc.value.details == {"name": "vmlinux", "part_number": 1}


def test_verify_chunks_checksum_mismatch_is_build_failure() -> None:
    entry = ManifestEntry(
        name="vmlinux",
        sha256="x",
        size_bytes=10,
        chunks=(ChunkEntry(sha256="c1", size_bytes=10),),
    )
    heads = {"p/vmlinux.part0001": HeadResult(size_bytes=10, checksum_sha256="WRONG", etag="e")}
    with pytest.raises(CategorizedError) as exc:
        verify_chunks(_FakeStore({}, heads), "p/", entry)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "uploaded chunk disagrees with its manifest"
    assert exc.value.details == {"name": "vmlinux", "part_number": 1}


def test_verify_chunks_size_mismatch_is_build_failure() -> None:
    entry = ManifestEntry(
        name="vmlinux",
        sha256="x",
        size_bytes=10,
        chunks=(ChunkEntry(sha256="c1", size_bytes=10),),
    )
    heads = {"p/vmlinux.part0001": HeadResult(size_bytes=11, checksum_sha256="c1", etag="e")}
    with pytest.raises(CategorizedError) as exc:
        verify_chunks(_FakeStore({}, heads), "p/", entry)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE


def test_chunked_artifact_skips_whole_object_checksum_but_checks_size() -> None:
    # A reassembled multipart object exposes only a composite checksum, so the whole-object
    # SHA-256 is NOT compared; only the total size is. A matching size passes even with a
    # differing whole-object checksum, while a size mismatch is a build failure.
    kblob = _bzimage()
    chunks = (ChunkEntry(sha256="c1", size_bytes=len(kblob)),)
    store = _FakeStore({"k": kblob}, {"k": _head(kblob, "DIFFERENT")})
    manifest = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob), chunks=chunks),
    ]
    result = validate_external_artifacts(
        store, manifest=manifest, keys={"kernel": "k"}, declared_build_id=None
    )
    assert result.output.kernel_ref == "k"

    store_bad = _FakeStore({"k": kblob}, {"k": _head(kblob, "DIFFERENT")})
    manifest_bad = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob) + 1, chunks=chunks),
    ]
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store_bad, manifest=manifest_bad, keys={"kernel": "k"}, declared_build_id=None
        )
    assert str(exc.value) == "reassembled artifact size disagrees with its manifest"
    assert exc.value.details == {"name": "kernel"}


def test_oversized_effective_config_message_and_details_and_boundary() -> None:
    # The size cap is read off the object head WITHOUT reading the body. A config exactly at the
    # cap is allowed; one byte over is a configuration error naming the size and the cap.
    cap = 1024 * 1024
    kblob = _bzimage()
    reqs = ConfigRequirements(required={})

    # At the cap: allowed (head reports exactly cap; body need not exist since size==cap is fine
    # and an empty required set means validate_config_requirements passes on whatever is read).
    at_cap_cfg = b"\n" * cap
    store_ok = _FakeStore(
        {"k": kblob, "c": at_cap_cfg},
        {"k": _head(kblob, "kc"), "c": HeadResult(size_bytes=cap, checksum_sha256="cc", etag="e")},
    )
    manifest = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob)),
        ManifestEntry(name="effective_config", sha256="cc", size_bytes=cap),
    ]
    validate_external_artifacts(
        store_ok,
        manifest=manifest,
        keys={"kernel": "k", "effective_config": "c"},
        declared_build_id=None,
        profile_requirements=reqs,
    )

    # One byte over the cap: rejected before any body read.
    store_bad = _FakeStore(
        {"k": kblob},
        {
            "k": _head(kblob, "kc"),
            "c": HeadResult(size_bytes=cap + 1, checksum_sha256="cc", etag="e"),
        },
    )
    manifest_bad = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob)),
        ManifestEntry(name="effective_config", sha256="cc", size_bytes=cap + 1),
    ]
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store_bad,
            manifest=manifest_bad,
            keys={"kernel": "k", "effective_config": "c"},
            declared_build_id=None,
            profile_requirements=reqs,
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "effective_config exceeds the readable size cap"
    assert exc.value.details == {
        "name": "effective_config",
        "size_bytes": cap + 1,
        "max_size_bytes": cap,
    }


def _elf_with_section(sh_size: int, sh_offset: int, *, blob_len: int) -> bytes:
    """ELF64-LE whose first real section declares (sh_offset, sh_size), padded to ``blob_len``.

    Used to exercise _read_section's caps via ranged reads without materializing huge blobs;
    the section is reached as the section-name string table (e_shstrndx points at it).
    """
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    e_shentsize = 64
    e_shnum = 2  # null section + the one under test
    e_shstrndx = 1
    sht_off = 64
    struct.pack_into("<Q", header, 0x28, sht_off)
    struct.pack_into("<H", header, 0x3A, e_shentsize)
    struct.pack_into("<H", header, 0x3C, e_shnum)
    struct.pack_into("<H", header, 0x3E, e_shstrndx)

    def section(sh_off: int, sh_sz: int) -> bytes:
        sh = bytearray(64)
        struct.pack_into("<Q", sh, 0x18, sh_off)
        struct.pack_into("<Q", sh, 0x20, sh_sz)
        return bytes(sh)

    sht = section(0, 0) + section(sh_offset, sh_size)
    blob = bytearray(bytes(header) + sht)
    if len(blob) < blob_len:
        blob.extend(b"\x00" * (blob_len - len(blob)))
    return bytes(blob)


def test_read_section_size_over_cap_is_build_failure() -> None:
    cap = 16 * 1024 * 1024
    # The shstrtab section declares a size one byte over the readable-section cap.
    blob = _elf_with_section(cap + 1, 128, blob_len=256)
    store = _FakeStore({"vmlinux": blob}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=10**12)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "vmlinux section exceeds the readable-section cap"
    assert exc.value.details == {"sh_size": cap + 1}


def test_read_section_extends_past_object_is_build_failure() -> None:
    # sh_size is within the cap but sh_offset+sh_size runs past max_size. The section header table
    # (offset 64, 2*64=128 bytes -> ends at 192) must still fit, so the section is placed beyond it.
    blob = _elf_with_section(50, 200, blob_len=512)
    store = _FakeStore({"vmlinux": blob}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=220)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "vmlinux section extends past the object size"
    assert exc.value.details == {"sh_offset": 200, "sh_size": 50}


def test_extract_build_id_ranged_oversized_sht_message_and_details() -> None:
    # e_shentsize * e_shnum exceeds the section cap: reported before the SHT is read. Both fields
    # are 16-bit, so pick a shentsize/shnum whose product trips the cap yet each fits 16 bits.
    cap = 16 * 1024 * 1024
    e_shentsize = 1024
    e_shnum = (cap // e_shentsize) + 1
    assert e_shnum <= 0xFFFF
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    struct.pack_into("<Q", header, 0x28, 64)
    struct.pack_into("<H", header, 0x3A, e_shentsize)
    struct.pack_into("<H", header, 0x3C, e_shnum)
    struct.pack_into("<H", header, 0x3E, 0)
    store = _FakeStore({"vmlinux": bytes(header)}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=10**12)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "vmlinux section header table exceeds the readable cap"
    assert exc.value.details == {"sht_bytes": e_shentsize * e_shnum}


def test_parse_gnu_build_id_note_shorter_than_header_is_rejected() -> None:
    # A blob with fewer than the 12 header bytes a note needs carries no parseable note.
    with pytest.raises(CategorizedError) as exc:
        parse_gnu_build_id(b"\x00" * 11)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "vmlinux carries no GNU build-id note"


def test_patch_target_paths_keeps_path_before_last_tab() -> None:
    # The path is everything before the FIRST tab; a second tab (e.g. a second timestamp field)
    # must not pull extra text into the path. rsplit would keep up to the LAST tab instead.
    patch = "--- a/fs/dcache.c\tts1\tts2\n+++ b/fs/dcache.c\tts1\tts2\n"
    assert patch_target_paths(patch, strip=1) == {Path("fs/dcache.c")}


def test_validate_external_artifacts_kernel_in_manifest_but_no_key_is_missing_kernel() -> None:
    # "kernel" present in the manifest but absent from keys must still be the headline
    # missing-kernel configuration error (manifest-OR-keys guard), not a per-artifact failure.
    blob = _bzimage()
    store = _FakeStore({}, {})
    manifest = [ManifestEntry(name="kernel", sha256="kc", size_bytes=len(blob))]
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(store, manifest=manifest, keys={}, declared_build_id=None)
    assert str(exc.value) == "external build is missing the required kernel artifact"


def test_bad_vmlinux_magic_is_build_failure() -> None:
    # _check_magic gates on the exact artifact name "vmlinux"; a vmlinux whose first bytes are not
    # the ELF magic is a build failure. A name-comparison that no longer matched "vmlinux" would
    # skip this check and wrongly admit a non-ELF vmlinux.
    kblob = _bzimage()
    vblob = b"\x00" * 64  # valid size, but no ELF magic
    store = _FakeStore(
        {"k": kblob, "v": vblob},
        {"k": _head(kblob, "kc"), "v": _head(vblob, "vc")},
    )
    manifest = [
        ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob)),
        ManifestEntry(name="vmlinux", sha256="vc", size_bytes=len(vblob)),
    ]
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store,
            manifest=manifest,
            keys={"kernel": "k", "vmlinux": "v"},
            declared_build_id="00",
        )
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "vmlinux is not an ELF file"
    assert exc.value.details == {"name": "vmlinux"}


def test_effective_config_present_key_but_missing_head_is_configuration_error() -> None:
    # key-present / head-absent (and the symmetric case) must each raise: the guard is OR, not AND.
    kblob = _bzimage()
    store = _FakeStore({"k": kblob}, {"k": _head(kblob, "kc")})
    manifest = [ManifestEntry(name="kernel", sha256="kc", size_bytes=len(kblob))]
    reqs = ConfigRequirements(required={})
    with pytest.raises(CategorizedError) as exc:
        validate_external_artifacts(
            store,
            manifest=manifest,
            # effective_config key supplied but the artifact was never uploaded (no head), and it
            # is not declared in the manifest, so heads has no effective_config entry.
            keys={"kernel": "k", "effective_config": "c"},
            declared_build_id=None,
            profile_requirements=reqs,
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "external build profile requirements need an effective_config artifact"


def test_structurally_malformed_elf_is_build_failure() -> None:
    # A header that passes the magic/class/endian checks and declares a section header table that
    # is in range, but whose SHT bytes are too short to unpack, trips struct.error -> the generic
    # "structurally malformed" build failure (exact message guards the None/case mutations).
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    struct.pack_into("<Q", header, 0x28, 64)  # e_shoff right after header
    struct.pack_into("<H", header, 0x3A, 64)  # e_shentsize
    struct.pack_into("<H", header, 0x3C, 1)  # e_shnum
    struct.pack_into("<H", header, 0x3E, 0)  # e_shstrndx
    # SHT region is in range (max_size large) but only 8 bytes long -> unpack at 0x18 fails.
    blob = bytes(header) + b"\x00" * 8
    store = _FakeStore({"vmlinux": blob}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=10**12)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert str(exc.value) == "vmlinux ELF is structurally malformed"


def _elf_header(*, e_shoff: int, e_shentsize: int, e_shnum: int) -> bytes:
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    struct.pack_into("<Q", header, 0x28, e_shoff)
    struct.pack_into("<H", header, 0x3A, e_shentsize)
    struct.pack_into("<H", header, 0x3C, e_shnum)
    struct.pack_into("<H", header, 0x3E, 0)
    return bytes(header)


def test_no_usable_sht_triggers_on_each_field_independently() -> None:
    # e_shoff == 0, e_shnum == 0, and e_shentsize < 64 each independently mean "no usable section
    # header table"; the guard ORs them and compares against the exact sentinels (0, 0, 64).
    for header in (
        _elf_header(e_shoff=0, e_shentsize=64, e_shnum=1),  # e_shoff == 0
        _elf_header(e_shoff=64, e_shentsize=64, e_shnum=0),  # e_shnum == 0
        _elf_header(e_shoff=64, e_shentsize=63, e_shnum=1),  # e_shentsize < 64
    ):
        store = _FakeStore({"vmlinux": header}, {})
        with pytest.raises(CategorizedError) as exc:
            extract_build_id_ranged(store, "vmlinux", max_size=10**12)
        assert str(exc.value) == "vmlinux has no usable section header table"


def test_parse_gnu_build_id_minimal_12_byte_header_note() -> None:
    # A note with a 12-byte header, name "GNU\0" (namesz 4), and a non-empty descriptor sits
    # exactly at the loop's lower bound; the build id is the descriptor bytes as hex.
    build_id = b"\xaa"
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    assert len(note) == 12 + 4 + 1
    assert parse_gnu_build_id(note) == "aa"


def _raw_note(name: bytes, desc: bytes, note_type: int) -> bytes:
    """Encode one ELF note with the same 4-byte alignment the parser assumes."""
    namesz = len(name)
    descsz = len(desc)
    name_pad = (-namesz) % 4
    desc_pad = (-descsz) % 4
    return (
        struct.pack("<III", namesz, descsz, note_type)
        + name
        + b"\x00" * name_pad
        + desc
        + b"\x00" * desc_pad
    )


def test_parse_gnu_build_id_handles_unaligned_name_padding() -> None:
    # A leading note whose name is not 4-byte aligned forces descriptor alignment padding; the GNU
    # note after it must still be located, which exercises the (-namesz % 4) / (-descsz % 4) math.
    build_id = b"\x12\x34"
    other = _raw_note(b"AB", b"\x01\x02\x03", note_type=0)  # namesz 2, descsz 3 (both unaligned)
    gnu = _raw_note(b"GNU", build_id, note_type=3)  # namesz 3 (unaligned), descsz 2
    assert parse_gnu_build_id(other + gnu) == "1234"


def test_read_section_exact_boundary_at_object_size_is_allowed() -> None:
    # sh_offset + sh_size == max_size is in-bounds (the check is strictly greater-than). The
    # shstrtab here ends exactly at max_size and is read without error, so extraction proceeds
    # to the (absent) build-id note and reports that, not a past-the-end failure.
    blob = _elf_with_section(50, 200, blob_len=512)
    store = _FakeStore({"vmlinux": blob}, {})
    with pytest.raises(CategorizedError) as exc:
        extract_build_id_ranged(store, "vmlinux", max_size=250)  # 200 + 50 == 250
    assert str(exc.value) == "vmlinux carries no .note.gnu.build-id section"
