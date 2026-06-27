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
from kdive.providers.shared.debug_common import debuginfo


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
            self, *, host: str, port: int, vmlinux_path: Path, transcript_path: Path
        ) -> GdbMiAttachment:
            seen["host"] = host
            seen["port"] = port
            seen["vmlinux_path"] = vmlinux_path
            seen["transcript_path"] = transcript_path
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
        "transcript_path": transcript_path,
        "vmlinux_path": staged_vmlinux,
    }
