"""Arch-parameterized guards for the byte-agnostic kernel-bundle host-side path (#1146, #1350).

The install plane's ``extract_kernel_bundle`` / ``_read_release`` stage whatever ``boot/vmlinuz``
bytes an already-validated upload carries (ADR-0343/0344) — a bzImage on x86_64, an ELF
``vmlinux`` on ppc64le. These tests lock that contract: the ELF boot member round-trips
byte-identically and a ``.ppc64le`` module version is handled, so the day someone re-adds a
bzImage-only assumption to the host-side path, ppc64le fails CI here.

``extract_kernel_bundle`` (ADR-0399) makes a *single* decompression pass over the combined tar,
extracting ``boot/vmlinuz`` and — when a ``modules_dest`` is given — repacking the ``lib/modules/``
subtree in the same pass; the two former helpers (``extract_boot_vmlinuz`` /
``repack_modules_subtree``) are gone. A guard here asserts the combined tar is opened once.

Scope: the *host-side tar I/O* only. Cross-arch module indexing (``depmod``) is covered separately
in ``test_module_indexing.py`` — as of #1148 it runs host-side, so it is no longer a libguestfs
cross-arch-execution question.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.boot import kernel_bundle
from kdive.providers.local_libvirt.lifecycle.boot.guest_kernel_writer import _RealGuestKernelWriter
from kdive.providers.local_libvirt.lifecycle.boot.kernel_bundle import (
    capped_tar_members,
    extract_kernel_bundle,
)

# Mirror the upload contract's magic (build_artifacts/validation.py) so a change there that the
# boot path should tolerate is exercised with matching bytes.
_ELF64LE_PREFIX = b"\x7fELF\x02\x01"  # magic + EI_CLASS=64-bit + EI_DATA=LE
_EM_PPC64_LE16 = (21).to_bytes(2, "little")  # e_machine == EM_PPC64 at offset 0x12

_X86_VERSION = "6.9.0-x86"
_PPC64LE_VERSION = "6.19.10-300.fc44.ppc64le"


def _x86_bzimage_boot_member() -> bytes:
    """A distinctive x86 boot-member blob (the bzImage `HdrS` magic sits at 0x202 in a real one)."""
    return b"bzImage-x86_64-payload-" + b"\x00" * 16


def _ppc64le_elf_boot_member() -> bytes:
    """A minimal ELF64-LE header pinned to ``EM_PPC64`` (offset 0x12) — the ppc64le boot member.

    Extraction is byte-agnostic, so the tests assert byte-identity, not ELF validity — but building
    a realistic ppc64le header documents what the member is and makes the guard bite a bzImage-only
    extractor (which would reject these bytes).
    """
    header = bytearray(64)
    header[: len(_ELF64LE_PREFIX)] = _ELF64LE_PREFIX
    header[0x12:0x14] = _EM_PPC64_LE16
    return bytes(header) + b"ppc64le-kernel-payload"


def _tar_add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _combined_tar(
    boot_bytes: bytes, *, member_name: str = "boot/vmlinuz", version: str = _X86_VERSION
) -> bytes:
    """The unified `kernel` artifact: gzip tar of the boot member + a lib/modules/<ver>/ subtree."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, member_name, boot_bytes)
        _tar_add(tar, f"lib/modules/{version}/modules.dep", b"")
        _tar_add(tar, f"lib/modules/{version}/kernel/drivers/virtio_blk.ko", b"module-bytes")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("arch", "boot_bytes"),
    [
        ("x86_64", _x86_bzimage_boot_member()),
        ("ppc64le", _ppc64le_elf_boot_member()),
    ],
)
def test_extract_kernel_bundle_round_trips_the_boot_member_byte_identically(
    arch: str, boot_bytes: bytes, tmp_path: Path
) -> None:
    """The extracted <kernel> file equals the boot member verbatim — no magic read, any arch.

    With ``modules_dest=None`` (the common non-kdump/non-debuginfo run) only the boot member is
    read: no modules tar is written and the found flag is False.
    """
    combined = tmp_path / "kernel.tar.gz"
    combined.write_bytes(_combined_tar(boot_bytes, version=f"1.0-{arch}"))
    dest = tmp_path / "kernel"

    found = extract_kernel_bundle(combined, dest, None)

    assert dest.read_bytes() == boot_bytes
    assert found is False


def test_extract_kernel_bundle_handles_a_dot_slash_prefixed_ppc64le_member(tmp_path: Path) -> None:
    """A ``./boot/vmlinuz`` member (leading ./) still resolves — covers ppc64le tar layouts."""
    boot_bytes = _ppc64le_elf_boot_member()
    combined = tmp_path / "kernel.tar.gz"
    combined.write_bytes(_combined_tar(boot_bytes, member_name="./boot/vmlinuz"))
    dest = tmp_path / "kernel"

    extract_kernel_bundle(combined, dest, None)

    assert dest.read_bytes() == boot_bytes


def test_extract_kernel_bundle_missing_boot_is_infrastructure_failure(tmp_path: Path) -> None:
    """A tar with no boot/vmlinuz member fails cleanly (arch-neutral error contract)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, f"lib/modules/{_PPC64LE_VERSION}/modules.dep", b"")
    combined = tmp_path / "kernel.tar.gz"
    combined.write_bytes(buf.getvalue())

    with pytest.raises(CategorizedError) as excinfo:
        extract_kernel_bundle(combined, tmp_path / "kernel", None)

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_extract_kernel_bundle_unreadable_tar_is_infrastructure_failure(tmp_path: Path) -> None:
    """A corrupt (non-gzip-tar) artifact is a clean INFRASTRUCTURE_FAILURE, not a raw TarError."""
    combined = tmp_path / "kernel.tar.gz"
    combined.write_bytes(b"not a gzip tar")

    with pytest.raises(CategorizedError) as excinfo:
        extract_kernel_bundle(combined, tmp_path / "kernel", None)

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize("version", [_X86_VERSION, _PPC64LE_VERSION])
def test_extract_kernel_bundle_repacks_the_modules_subtree(version: str, tmp_path: Path) -> None:
    """With a modules_dest the same pass repacks lib/modules/<ver> and _read_release round-trips.

    The ``.ppc64le`` arch suffix in the module version is part of the string and must survive
    round-trip untouched — a naive suffix-strip would corrupt the injected /lib/modules/<ver> path.
    """
    boot_bytes = _ppc64le_elf_boot_member()
    combined = tmp_path / "kernel.tar.gz"
    combined.write_bytes(_combined_tar(boot_bytes, version=version))
    kernel_dest = tmp_path / "kernel"
    modules_tar = tmp_path / "modules.tar.gz"

    assert extract_kernel_bundle(combined, kernel_dest, modules_tar) is True

    assert kernel_dest.read_bytes() == boot_bytes  # boot still extracted in the same pass
    with tarfile.open(modules_tar, "r:gz") as archive:
        names = archive.getnames()
    assert any(name.startswith(f"lib/modules/{version}/") for name in names)
    assert _RealGuestKernelWriter._read_release(modules_tar, "overlay") == version


def test_extract_kernel_bundle_returns_false_when_no_modules_present(tmp_path: Path) -> None:
    """A boot-only tar with a modules_dest repacks nothing (the modules-absent path)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, "boot/vmlinuz", _ppc64le_elf_boot_member())
    combined = tmp_path / "kernel.tar.gz"
    combined.write_bytes(buf.getvalue())
    modules_tar = tmp_path / "modules.tar.gz"

    assert extract_kernel_bundle(combined, tmp_path / "kernel", modules_tar) is False
    assert not modules_tar.exists()


def test_extract_kernel_bundle_opens_the_combined_tar_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Performance-regression guard (ADR-0399): the combined tar is decompressed once, not twice.

    Count reads of the *combined-tar path* specifically — the merged pass also opens the repacked
    modules tar in write mode, so a bare total open count is 2, not 1.
    """
    combined = tmp_path / "kernel.tar.gz"
    combined.write_bytes(_combined_tar(_ppc64le_elf_boot_member(), version=_PPC64LE_VERSION))
    modules_tar = tmp_path / "modules.tar.gz"

    real_open = tarfile.open
    combined_read_opens = 0

    def _counting_open(name=None, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal combined_read_opens
        if Path(str(name)) == combined and mode.startswith("r"):
            combined_read_opens += 1
        return real_open(name, mode, *args, **kwargs)

    monkeypatch.setattr(kernel_bundle.tarfile, "open", _counting_open)

    extract_kernel_bundle(combined, tmp_path / "kernel", modules_tar)

    assert combined_read_opens == 1


def test_capped_tar_members_rejects_a_member_count_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # getmembers()/getnames() eagerly build one TarInfo per member, so a header bomb would OOM the
    # worker before any content is read. The lazy capped iterator rejects past the bound (#1148).
    monkeypatch.setattr(kernel_bundle, "MAX_KERNEL_TAR_MEMBERS", 2)
    tar_path = tmp_path / "many.tar.gz"
    with tarfile.open(tar_path, "w:gz") as out:
        for i in range(5):
            _tar_add(out, f"lib/modules/{_PPC64LE_VERSION}/m{i}.ko", b"x")
    with tarfile.open(tar_path, "r:gz") as archive, pytest.raises(CategorizedError) as exc:
        list(capped_tar_members(archive))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_extract_kernel_bundle_rejects_an_oversize_boot_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A boot member declaring a huge size is a decompression bomb: reject before read() allocates.
    monkeypatch.setattr(kernel_bundle, "MAX_KERNEL_TAR_UNCOMPRESSED_BYTES", 4)
    combined = tmp_path / "kernel.tar.gz"
    combined.write_bytes(_combined_tar(_ppc64le_elf_boot_member()))
    with pytest.raises(CategorizedError) as exc:
        extract_kernel_bundle(combined, tmp_path / "kernel", None)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_extract_kernel_bundle_rejects_an_oversize_module_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cumulative module-tree size over the bound is rejected, and the partial temp tar is cleaned.
    # The bound is set above the boot member but below the module tree so the boot extract succeeds
    # first and only the modules pass trips the cap.
    monkeypatch.setattr(kernel_bundle, "MAX_KERNEL_TAR_UNCOMPRESSED_BYTES", 100)
    combined = tmp_path / "kernel.tar.gz"
    big_modules = io.BytesIO()
    with tarfile.open(fileobj=big_modules, mode="w:gz") as tar:
        _tar_add(tar, "boot/vmlinuz", _ppc64le_elf_boot_member())
        _tar_add(tar, f"lib/modules/{_PPC64LE_VERSION}/big.ko", b"x" * 200)
    combined.write_bytes(big_modules.getvalue())
    dest = tmp_path / "modules.tar.gz"
    with pytest.raises(CategorizedError) as exc:
        extract_kernel_bundle(combined, tmp_path / "kernel", dest)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not dest.with_name(dest.name + ".part").exists()
