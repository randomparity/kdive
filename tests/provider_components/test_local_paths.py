from __future__ import annotations

from pathlib import Path

import pytest

from kdive.components.local_paths import validate_local_component_path
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_accepts_regular_file_under_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")

    result = validate_local_component_path(str(image), allowed_roots=[root])

    assert result == image.resolve()


def test_rejects_path_outside_allowed_roots(tmp_path: Path) -> None:
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"data")

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(outside), allowed_roots=[tmp_path / "root"])

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.qcow2"
    root.mkdir()
    outside.write_bytes(b"data")
    (root / "link.qcow2").symlink_to(outside)

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(root / "link.qcow2"), allowed_roots=[root])

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_rejects_sha256_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(image), allowed_roots=[root], sha256="sha256:" + "0" * 64)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_digest_read_failure_maps_to_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    image = root / "disk.qcow2"
    image.write_bytes(b"content")

    def fail_digest(_path: Path) -> str:
        raise OSError("read race")

    monkeypatch.setattr("kdive.components.local_paths._file_sha256", fail_digest)
    with pytest.raises(CategorizedError) as exc_info:
        validate_local_component_path(str(image), allowed_roots=[root], sha256="sha256:" + "0" * 64)
    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR
