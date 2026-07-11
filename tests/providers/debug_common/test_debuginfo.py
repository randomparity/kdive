"""Unit tests for the provider-neutral debuginfo resolver + staging seam (#702/#707).

The orchestration (ref lookup, the ``no_debuginfo`` error, the private staging dir, and its
removal on any failure) is driven directly with injected seams; only the real DB read, object-store
fetch, and gdb spawn are ``live_vm``-real and not exercised here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.debug import GdbMiAttachment
from kdive.providers.shared.debug_common.gdbmi import debuginfo


class _RecordingFetch:
    """A fake object-store fetch that records its ref args and returns canned bytes (or raises)."""

    def __init__(self, data: bytes = b"", error: CategorizedError | None = None) -> None:
        self._data = data
        self._error = error
        self.refs: list[str] = []

    def __call__(self, ref: str) -> bytes:
        self.refs.append(ref)
        if self._error is not None:
            raise self._error
        return self._data


def test_resolve_fetches_present_ref_to_dest(tmp_path: Path) -> None:
    fetch = _RecordingFetch(data=b"ELFDATA")
    resolver = debuginfo.DebuginfoResolver(
        read_debuginfo_ref=lambda run_id: "runs/r1/vmlinux", fetch_object=fetch
    )
    dest = tmp_path / "vmlinux"
    result = resolver.resolve("r1", dest)
    assert result == dest
    assert dest.read_bytes() == b"ELFDATA"
    assert fetch.refs == ["runs/r1/vmlinux"]


def test_resolve_none_ref_raises_no_debuginfo_before_fetch(tmp_path: Path) -> None:
    fetch = _RecordingFetch(data=b"unused")
    resolver = debuginfo.DebuginfoResolver(
        read_debuginfo_ref=lambda run_id: None, fetch_object=fetch
    )
    dest = tmp_path / "vmlinux"
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve("r1", dest)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"run_id": "r1", "reason": "no_debuginfo"}
    assert str(exc.value) == (
        "the Run has no published debuginfo object; build the kernel before attaching gdb"
    )
    assert fetch.refs == []  # the absent debuginfo is caught before any fetch
    assert not dest.exists()


def test_resolve_propagates_fetch_error(tmp_path: Path) -> None:
    boom = CategorizedError(
        "object store unreachable", category=ErrorCategory.INFRASTRUCTURE_FAILURE
    )
    fetch = _RecordingFetch(error=boom)
    resolver = debuginfo.DebuginfoResolver(
        read_debuginfo_ref=lambda run_id: "runs/r1/vmlinux", fetch_object=fetch
    )
    dest = tmp_path / "vmlinux"
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve("r1", dest)
    assert exc.value is boom  # re-raised unchanged
    assert not dest.exists()


def test_resolve_writes_to_dest_not_run_id_derived_path(tmp_path: Path) -> None:
    # The resolver writes where it is told; it computes no run_id-derived path itself (the private
    # per-attach staging dir is the seam's responsibility). A hostile run_id never reaches the path.
    fetch = _RecordingFetch(data=b"SYMBOLS")
    resolver = debuginfo.DebuginfoResolver(
        read_debuginfo_ref=lambda run_id: "key", fetch_object=fetch
    )
    dest = tmp_path / "custom-name"
    resolver.resolve("../../etc/passwd", dest)
    assert dest.read_bytes() == b"SYMBOLS"
    assert dest.parent == tmp_path


def _fake_attachment() -> GdbMiAttachment:
    return cast(GdbMiAttachment, object())


def test_stage_and_attach_stages_into_private_dir_and_attaches(tmp_path: Path) -> None:
    fetch = _RecordingFetch(data=b"ELF")
    resolver = debuginfo.DebuginfoResolver(
        read_debuginfo_ref=lambda run_id: "key", fetch_object=fetch
    )
    seen: dict[str, Path] = {}
    sentinel = _fake_attachment()

    def attach(vmlinux_path: Path) -> GdbMiAttachment:
        seen["path"] = vmlinux_path
        # The staged vmlinux exists and is readable at attach time.
        assert vmlinux_path.read_bytes() == b"ELF"
        return sentinel

    result = debuginfo.stage_and_attach(run_id="r1", attach=attach, resolver=resolver)
    assert result is sentinel
    staged = seen["path"]
    assert staged.name == "vmlinux"
    # Private per-attach dir (mkdtemp default 0o700), not a fixed/predictable name.
    assert staged.parent.name.startswith("kdive-debuginfo-")
    assert (staged.parent.stat().st_mode & 0o777) == 0o700
    # Successful attach keeps the staged vmlinux for the live gdb session's lifetime.
    assert staged.exists()


def test_stage_and_attach_removes_staging_dir_on_resolve_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(debuginfo.tempfile, "tempdir", str(tmp_path))
    resolver = debuginfo.DebuginfoResolver(
        read_debuginfo_ref=lambda run_id: None, fetch_object=_RecordingFetch()
    )

    def attach(_vmlinux_path: Path) -> GdbMiAttachment:  # pragma: no cover - never reached
        raise AssertionError("attach must not run when resolve fails")

    with pytest.raises(CategorizedError) as exc:
        debuginfo.stage_and_attach(run_id="r1", attach=attach, resolver=resolver)
    assert exc.value.details == {"run_id": "r1", "reason": "no_debuginfo"}
    # The staging dir was reclaimed on failure: nothing is left under the temp root.
    assert list(tmp_path.iterdir()) == []


def test_stage_and_attach_removes_staging_dir_on_attach_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(debuginfo.tempfile, "tempdir", str(tmp_path))
    resolver = debuginfo.DebuginfoResolver(
        read_debuginfo_ref=lambda run_id: "key", fetch_object=_RecordingFetch(data=b"ELF")
    )
    boom = CategorizedError("gdb attach failed", category=ErrorCategory.DEBUG_ATTACH_FAILURE)

    def attach(_vmlinux_path: Path) -> GdbMiAttachment:
        raise boom

    with pytest.raises(CategorizedError) as exc:
        debuginfo.stage_and_attach(run_id="r1", attach=attach, resolver=resolver)
    assert exc.value is boom
    # The staging dir was reclaimed on attach failure: nothing is left under the temp root.
    assert list(tmp_path.iterdir()) == []


def test_gdb_attach_seam_uses_engine_factory_with_staged_vmlinux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel = _fake_attachment()
    seen: dict[str, object] = {}
    staged_vmlinux = tmp_path / "vmlinux"
    transcript_path = tmp_path / "mi.log"

    class FakeEngine:
        def attach(
            self, *, host: str, port: int, vmlinux_path: Path, transcript_path: Path, run_id: str
        ) -> GdbMiAttachment:
            seen["host"] = host
            seen["port"] = port
            seen["vmlinux_path"] = vmlinux_path
            seen["transcript_path"] = transcript_path
            seen["engine_run_id"] = run_id
            return sentinel

    def fake_stage_and_attach(
        *,
        run_id: str,
        attach: Callable[[Path], GdbMiAttachment],
        resolver: debuginfo.DebuginfoResolver | None = None,
    ) -> GdbMiAttachment:
        assert resolver is None
        seen["run_id"] = run_id
        return attach(staged_vmlinux)

    monkeypatch.setattr(debuginfo, "stage_and_attach", fake_stage_and_attach)

    seam = debuginfo.gdb_attach_seam(engine_factory=FakeEngine)
    result = seam(host="127.0.0.1", port=1234, run_id="r1", transcript_path=transcript_path)

    assert result is sentinel
    assert seen == {
        "host": "127.0.0.1",
        "port": 1234,
        "run_id": "r1",
        "engine_run_id": "r1",  # run_id is threaded into engine.attach (ADR-0278, #923)
        "transcript_path": transcript_path,
        "vmlinux_path": staged_vmlinux,
    }


# --- ModuleDebuginfoResolver (#923, ADR-0278) ----------------------------------------------

import io  # noqa: E402
import struct  # noqa: E402
import tarfile  # noqa: E402


def _make_modules_tar(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_module_resolve_returns_path_and_identity() -> None:
    tar = _make_modules_tar({"lib/modules/6.0/kernel/fs/foo.ko": b"ELF-foo"})
    fetch = _RecordingFetch(data=tar)
    resolver = debuginfo.ModuleDebuginfoResolver(
        read_kernel_ref=lambda run_id: "runs/r1/kernel.tar",
        fetch_object=fetch,
        read_identity=lambda path: ("SRC123", "BID456"),
    )
    result = resolver.resolve("r1", "foo")
    assert result.path.read_bytes() == b"ELF-foo"
    assert result.srcversion == "SRC123"
    assert result.build_id == "BID456"
    assert fetch.refs == ["runs/r1/kernel.tar"]


def test_module_resolve_absent_ko_raises_no_module_debuginfo() -> None:
    tar = _make_modules_tar({"lib/modules/6.0/kernel/fs/other.ko": b"x"})
    resolver = debuginfo.ModuleDebuginfoResolver(
        read_kernel_ref=lambda run_id: "runs/r1/kernel.tar",
        fetch_object=_RecordingFetch(data=tar),
        read_identity=lambda path: (None, None),
    )
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve("r1", "foo")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["reason"] == "no_module_debuginfo"
    assert exc.value.details["module"] == "foo"
    assert "CONFIG_DEBUG_INFO" in str(exc.value)


def test_module_resolve_matches_dash_underscore_variant() -> None:
    tar = _make_modules_tar({"lib/modules/6.0/kernel/foo-bar.ko": b"FB"})
    resolver = debuginfo.ModuleDebuginfoResolver(
        read_kernel_ref=lambda run_id: "k",
        fetch_object=_RecordingFetch(data=tar),
        read_identity=lambda path: (None, None),
    )
    result = resolver.resolve("r1", "foo_bar")
    assert result.path.read_bytes() == b"FB"


def test_module_resolve_caches_fetch_per_run() -> None:
    tar = _make_modules_tar({"lib/modules/6.0/foo.ko": b"f"})
    fetch = _RecordingFetch(data=tar)
    resolver = debuginfo.ModuleDebuginfoResolver(
        read_kernel_ref=lambda run_id: "k",
        fetch_object=fetch,
        read_identity=lambda path: (None, None),
    )
    resolver.resolve("r1", "foo")
    resolver.resolve("r1", "foo")
    assert len(fetch.refs) == 1


def test_module_resolve_no_kernel_ref_raises_no_module_debuginfo() -> None:
    resolver = debuginfo.ModuleDebuginfoResolver(
        read_kernel_ref=lambda run_id: None,
        fetch_object=_RecordingFetch(data=b"unused"),
        read_identity=lambda path: (None, None),
    )
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve("r1", "foo")
    assert exc.value.details["reason"] == "no_module_debuginfo"


# --- parse_module_identity (pure ELF parse) -------------------------------------------------


def _make_ko_elf(*, srcversion: bytes | None, build_id: bytes | None) -> bytes:
    shstrtab = b"\x00.shstrtab\x00.modinfo\x00.note.gnu.build-id\x00"
    modinfo = b"license=GPL\x00"
    if srcversion is not None:
        modinfo += b"srcversion=" + srcversion + b"\x00"
    note = b""
    if build_id is not None:
        note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id

    blobs = [(b"", b""), (b".shstrtab", shstrtab), (b".modinfo", modinfo)]
    if build_id is not None:
        blobs.append((b".note.gnu.build-id", note))

    name_off = {b".shstrtab": 1, b".modinfo": 11, b".note.gnu.build-id": 20, b"": 0}
    body = bytearray()
    spans: list[tuple[int, int]] = []
    cursor = 64
    for _name, data in blobs:
        spans.append((cursor, len(data)))
        body += data
        cursor += len(data)
    sh_off = 64 + len(body)

    shdrs = bytearray()
    for (name, _data), (offset, size) in zip(blobs, spans, strict=True):
        shdrs += struct.pack("<IIQQQQIIQQ", name_off[name], 1, 0, 0, offset, size, 0, 0, 0, 0)

    shnum = len(blobs)
    shstrndx = 1
    ehdr = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    ehdr += struct.pack("<HHIQQQIHHHHHH", 1, 62, 1, 0, 0, sh_off, 0, 64, 0, 0, 64, shnum, shstrndx)
    return bytes(ehdr) + bytes(body) + bytes(shdrs)


def test_parse_module_identity_reads_srcversion_and_build_id() -> None:
    elf = _make_ko_elf(srcversion=b"ABCD1234", build_id=b"\xde\xad\xbe\xef")
    assert debuginfo.parse_module_identity(elf) == ("ABCD1234", "deadbeef")


def test_parse_module_identity_missing_build_id_returns_none() -> None:
    elf = _make_ko_elf(srcversion=b"SRC", build_id=None)
    assert debuginfo.parse_module_identity(elf) == ("SRC", None)


def test_parse_module_identity_non_elf_returns_none() -> None:
    assert debuginfo.parse_module_identity(b"not an elf file at all") == (None, None)
