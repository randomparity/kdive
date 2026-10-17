"""Tests for the ``kdive stage-volume`` orchestration and local config capture (ADR-0336)."""

from __future__ import annotations

import ast
import hashlib
import inspect
from importlib.util import resolve_name
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.rootfs import stage_volume as stage_volume_module
from kdive.images.rootfs.stage_volume import (
    StageVolumeDeps,
    _TargetRow,
    capture_kernel_config,
    stage_volume,
)
from kdive.providers.ports.external_boot import RootSource, RootSpecV1

_ROW_ID = uuid4()
_VOLUME = "fedora-44.qcow2"


def _image(tmp_path: Path, *, header: bytes | None = None) -> Path:
    path = tmp_path / "img.qcow2"
    path.write_bytes(
        header
        if header is not None
        else b"QFI\xfb" + (2).to_bytes(4, "big") + bytes(64) + b"payload"
    )
    return path


def _is_provider_module(name: str) -> bool:
    """Whether ``name`` is the provider package or one of its submodules."""
    return name == "kdive.providers" or name.startswith("kdive.providers.")


def _provider_imports(tree: ast.AST, package: str) -> list[str]:
    """Return provider modules imported by ``tree``, resolving relative imports from ``package``."""
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            resolved = (
                resolve_name(f"{'.' * node.level}{module}", package) if node.level else module
            )
            candidates = [resolved]
            if not _is_provider_module(resolved):
                candidates.extend(f"{resolved}.{alias.name}" for alias in node.names)
        else:
            continue
        imports.extend(candidate for candidate in candidates if _is_provider_module(candidate))
    return imports


def test_stage_volume_module_remains_provider_neutral() -> None:
    """The neutral orchestration owns no CLI assembly or provider composition imports."""
    module = inspect.getmodule(stage_volume)
    assert module is stage_volume_module
    assert not hasattr(module, "add_stage_volume_parser")
    assert not hasattr(module, "run_stage_volume")

    package = module.__package__
    assert package is not None
    assert _provider_imports(ast.parse(inspect.getsource(module)), package) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("import kdive.providers", ["kdive.providers"], id="direct-provider"),
        pytest.param(
            "import kdive.providers.remote_libvirt",
            ["kdive.providers.remote_libvirt"],
            id="direct-provider-submodule",
        ),
        pytest.param(
            "from ...providers import remote_libvirt",
            ["kdive.providers"],
            id="relative-provider",
        ),
        pytest.param(
            "from ... import providers",
            ["kdive.providers"],
            id="relative-package-provider",
        ),
        pytest.param("import kdive.providers_extra", [], id="non-provider-near-match"),
    ],
)
def test_stage_volume_provider_import_detector_catches_all_import_forms(
    source: str, expected: list[str]
) -> None:
    """The layering guard resolves absolute and package-relative provider imports."""
    package = stage_volume_module.__package__
    assert package is not None
    assert _provider_imports(ast.parse(source), package) == expected


class _Recorder:
    """Records the orchestration's calls, in order, and lets each seam be scripted to fail."""

    def __init__(
        self,
        *,
        row: _TargetRow | None = None,
        config: bytes | None = b"CONFIG_X=y\n",
        find_error: CategorizedError | None = None,
        upload_error: CategorizedError | None = None,
        attach_error: CategorizedError | None = None,
        inspect_error: CategorizedError | None = None,
        persist_error: CategorizedError | None = None,
    ) -> None:
        self._row = row or _TargetRow(row_id=_ROW_ID, volume=_VOLUME)
        self._config = config
        self._find_error = find_error
        self._upload_error = upload_error
        self._attach_error = attach_error
        self._inspect_error = inspect_error
        self._persist_error = persist_error
        self.inspected: tuple[Path, bytes, str] | None = None
        self.uploaded_bytes: bytes | None = None
        self.persisted: tuple[_TargetRow, str, dict[str, object]] | None = None
        self.calls: list[str] = []
        self.uploaded: tuple[str, Path] | None = None
        self.attached: tuple[str, str, str, UUID, bytes] | None = None

    def find_row(self, provider: str, name: str, arch: str) -> _TargetRow:
        self.calls.append("find")
        if self._find_error is not None:
            raise self._find_error
        return self._row

    def capture_config(self, qcow2: Path) -> bytes | None:
        self.calls.append("capture")
        return self._config

    def inspect_root(
        self, path: Path, arch: Literal["x86_64", "ppc64le"], digest: str
    ) -> RootSpecV1:
        self.calls.append("inspect")
        if self._inspect_error:
            raise self._inspect_error
        self.inspected = (path, path.read_bytes(), digest)
        return RootSpecV1(
            architecture=arch,
            root="UUID=abc",
            arguments=("root=UUID=abc",),
            authority="stage-inspection",
            source=RootSource(kind="staged-image", identity=digest),
        )

    def persist_root(
        self,
        provider: str,
        name: str,
        arch: str,
        target: _TargetRow,
        digest: str,
        root: dict[str, object],
    ) -> None:
        self.calls.append("persist")
        if self._persist_error:
            raise self._persist_error
        self.persisted = (target, digest, root)

    def upload_volume(self, volume: str, qcow2: Path) -> None:
        self.calls.append("upload")
        if self._upload_error is not None:
            raise self._upload_error
        self.uploaded = (volume, qcow2)
        self.uploaded_bytes = qcow2.read_bytes()

    def attach_config(
        self, provider: str, name: str, arch: str, row_id: UUID, config: bytes
    ) -> None:
        self.calls.append("attach")
        if self._attach_error is not None:
            raise self._attach_error
        self.attached = (provider, name, arch, row_id, config)

    def deps(self) -> StageVolumeDeps:
        return StageVolumeDeps(
            find_row=self.find_row,
            capture_config=self.capture_config,
            upload_volume=self.upload_volume,
            attach_config=self.attach_config,
            inspect_root=self.inspect_root,
            persist_root=self.persist_root,
        )


def test_stage_volume_happy_path_uploads_then_attaches(tmp_path: Path) -> None:
    """Row resolved, config captured, volume uploaded, config attached — in order."""
    rec = _Recorder()
    stage_volume("remote-libvirt", "fedora-44", "x86_64", _image(tmp_path), rec.deps())
    assert rec.calls == ["find", "inspect", "capture", "upload", "persist", "attach"]
    assert rec.inspected is not None
    snapshot, data, digest = rec.inspected
    assert rec.uploaded == (_VOLUME, snapshot)
    assert rec.uploaded_bytes == data
    assert digest == "sha256:" + hashlib.sha256(data).hexdigest()
    assert rec.persisted is not None and rec.persisted[1] == digest
    assert rec.persisted[2]["source"] == {"kind": "staged-image", "identity": digest}
    assert snapshot != tmp_path / "img.qcow2" and not snapshot.exists()
    assert rec.attached == ("remote-libvirt", "fedora-44", "x86_64", _ROW_ID, b"CONFIG_X=y\n")


def test_stage_volume_missing_row_fails_before_upload(tmp_path: Path) -> None:
    """A missing catalog row fails fast — no volume is uploaded for an unknown image."""
    rec = _Recorder(
        find_error=CategorizedError("no row", category=ErrorCategory.CONFIGURATION_ERROR)
    )
    with pytest.raises(CategorizedError):
        stage_volume("remote-libvirt", "ghost", "x86_64", _image(tmp_path), rec.deps())
    assert rec.calls == ["find"]
    assert rec.uploaded is None


def test_stage_volume_upload_failure_is_fatal_and_skips_attach(tmp_path: Path) -> None:
    """A volume-upload fault propagates (fatal) and no config is attached."""
    rec = _Recorder(
        upload_error=CategorizedError("boom", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    )
    with pytest.raises(CategorizedError):
        stage_volume("remote-libvirt", "fedora-44", "x86_64", _image(tmp_path), rec.deps())
    assert rec.calls == ["find", "inspect", "capture", "upload"]
    assert rec.attached is None


def test_stage_volume_no_config_uploads_without_attach(tmp_path: Path) -> None:
    """A None captured config stages the volume with no offer (no attach)."""
    rec = _Recorder(config=None)
    stage_volume("remote-libvirt", "fedora-44", "x86_64", _image(tmp_path), rec.deps())
    assert rec.calls == ["find", "inspect", "capture", "upload", "persist"]
    assert rec.uploaded is not None
    assert rec.attached is None


def test_stage_volume_attach_failure_is_advisory(tmp_path: Path) -> None:
    """An attach failure after a successful upload is advisory — the command does not raise."""
    rec = _Recorder(
        attach_error=CategorizedError("s3 down", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    )
    stage_volume("remote-libvirt", "fedora-44", "x86_64", _image(tmp_path), rec.deps())
    assert rec.calls == ["find", "inspect", "capture", "upload", "persist", "attach"]
    assert rec.uploaded is not None  # the volume landed despite the attach failure


# --- capture_kernel_config ---------------------------------------------------


def test_capture_kernel_config_single_kernel_reads_config(tmp_path: Path) -> None:
    """Exactly one non-rescue kernel -> the matching /boot/config version is read."""
    seen: list[str] = []

    def _boot(_qcow2: Path) -> list[str]:
        return ["vmlinuz-6.9.0", "config-6.9.0", "initramfs-6.9.0.img"]

    def _config(_qcow2: Path, version: str) -> bytes:
        seen.append(version)
        return b"CONFIG_DEBUG_INFO_BTF=y\n"

    out = capture_kernel_config(
        tmp_path / "img.qcow2", boot_entries_probe=_boot, kernel_config_probe=_config
    )
    assert out == b"CONFIG_DEBUG_INFO_BTF=y\n"
    assert seen == ["6.9.0"]


def test_capture_kernel_config_many_kernels_returns_none(tmp_path: Path) -> None:
    """Two baseline kernels are ambiguous — no version, no config probe."""

    def _boot(_qcow2: Path) -> list[str]:
        return ["vmlinuz-6.9.0", "vmlinuz-6.8.0"]

    def _config(_qcow2: Path, _version: str) -> bytes:  # pragma: no cover - must not be called
        raise AssertionError("config probe must not run for ambiguous /boot")

    assert (
        capture_kernel_config(
            tmp_path / "img.qcow2", boot_entries_probe=_boot, kernel_config_probe=_config
        )
        is None
    )


def test_capture_kernel_config_probe_error_degrades_to_none(tmp_path: Path) -> None:
    """A probe CategorizedError is advisory -> None (never propagates)."""

    def _boot(_qcow2: Path) -> list[str]:
        raise CategorizedError("no guestfish", category=ErrorCategory.MISSING_DEPENDENCY)

    def _config(_qcow2: Path, _version: str) -> bytes:  # pragma: no cover
        raise AssertionError("unreached")

    assert (
        capture_kernel_config(
            tmp_path / "img.qcow2", boot_entries_probe=_boot, kernel_config_probe=_config
        )
        is None
    )


def test_capture_kernel_config_absent_listing_returns_none(tmp_path: Path) -> None:
    """An unproduceable /boot listing (None) degrades to no config."""

    def _boot(_qcow2: Path) -> None:
        return None

    def _config(_qcow2: Path, _version: str) -> bytes:  # pragma: no cover
        raise AssertionError("unreached")

    assert (
        capture_kernel_config(
            tmp_path / "img.qcow2", boot_entries_probe=_boot, kernel_config_probe=_config
        )
        is None
    )


@pytest.mark.parametrize("stage", ["inspect", "persist"])
def test_mandatory_root_failure_is_fatal(tmp_path: Path, stage: str) -> None:
    fault = CategorizedError("root failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    rec = _Recorder(
        inspect_error=fault if stage == "inspect" else None,
        persist_error=fault if stage == "persist" else None,
    )
    with pytest.raises(CategorizedError):
        stage_volume("remote-libvirt", "base", "x86_64", _image(tmp_path), rec.deps())
    assert rec.attached is None and rec.persisted is None
    assert (rec.uploaded is None) == (stage == "inspect")
    if rec.inspected:
        assert not rec.inspected[0].exists()


@pytest.mark.parametrize(
    "version,backing,features", [(2, 1, 0), (3, 1, 0), (3, 0, 4), (3, 0, 32), (1, 0, 0)]
)
def test_rejects_external_or_unknown_qcow2_before_inspection(
    tmp_path: Path, version: int, backing: int, features: int
) -> None:
    header = bytearray(104)
    header[:4] = b"QFI\xfb"
    header[4:8] = version.to_bytes(4, "big")
    header[8:16] = backing.to_bytes(8, "big")
    header[72:80] = features.to_bytes(8, "big")
    rec = _Recorder()
    with pytest.raises(CategorizedError, match="self-contained"):
        stage_volume(
            "remote-libvirt", "base", "x86_64", _image(tmp_path, header=bytes(header)), rec.deps()
        )
    assert rec.calls == ["find"]


def test_snapshot_survives_source_replacement(tmp_path: Path) -> None:
    source = _image(tmp_path)
    original = source.read_bytes()
    rec = _Recorder()
    deps = rec.deps()

    def inspect(path: Path, arch: Literal["x86_64", "ppc64le"], digest: str) -> RootSpecV1:
        source.write_bytes(b"replaced source")
        return rec.inspect_root(path, arch, digest)

    from dataclasses import replace

    stage_volume("remote-libvirt", "base", "x86_64", source, replace(deps, inspect_root=inspect))
    assert rec.uploaded_bytes == original
    assert rec.persisted is not None
    assert rec.persisted[1] == "sha256:" + hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("arch", ["x86_64", "ppc64le"])
def test_snapshot_is_private_and_bounded_hash_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arch: str
) -> None:
    source = _image(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(stage_volume_module, "_CHUNK_BYTES", 7)
    from dataclasses import replace

    original = rec.deps()

    def inspect(path: Path, architecture: Literal["x86_64", "ppc64le"], digest: str) -> RootSpecV1:
        assert path.stat().st_mode & 0o777 == 0o400
        assert path.parent.stat().st_mode & 0o777 == 0o700
        return rec.inspect_root(path, architecture, digest)

    stage_volume("remote-libvirt", "base", arch, source, replace(original, inspect_root=inspect))
    assert rec.persisted is not None
    assert rec.persisted[1] == "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "header", [b"", b"notqcow2", b"QFI\xfb" + (3).to_bytes(4, "big") + bytes(64)]
)
def test_malformed_header_never_reaches_inspection(tmp_path: Path, header: bytes) -> None:
    rec = _Recorder()
    with pytest.raises(CategorizedError, match="self-contained"):
        stage_volume(
            "remote-libvirt", "base", "x86_64", _image(tmp_path, header=header), rec.deps()
        )
    assert rec.calls == ["find"]


def test_mismatched_inspector_identity_cannot_upload(tmp_path: Path) -> None:
    from dataclasses import replace

    rec = _Recorder()

    def inspect(path: Path, arch: Literal["x86_64", "ppc64le"], digest: str) -> RootSpecV1:
        return rec.inspect_root(path, arch, "sha256:" + "0" * 64)

    with pytest.raises(CategorizedError, match="does not match"):
        stage_volume(
            "remote-libvirt",
            "base",
            "x86_64",
            _image(tmp_path),
            replace(rec.deps(), inspect_root=inspect),
        )
    assert rec.uploaded is None and rec.persisted is None
