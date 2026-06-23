from __future__ import annotations

import os
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
    assert str(caught.value) == "local component path is outside provider allowed roots"


def test_outside_roots_rejection_enumerates_accepted_values(tmp_path: Path) -> None:
    # The rejection carries the configured roots so a black-box MCP caller can self-correct
    # without host access (#731, ADR-0224). Roots are matched against the code's own
    # strict=False resolution so the asserted strings equal what the code emits.
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"data")

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(outside), allowed_roots=[root_b, root_a])

    expected = sorted([str(root_a.resolve()), str(root_b.resolve())])
    assert caught.value.details["accepted_values"] == expected


def test_outside_roots_accepted_values_leaks_no_caller_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"data")

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(outside), allowed_roots=[root])

    accepted = caught.value.details["accepted_values"]
    assert isinstance(accepted, list)
    # Only the configured roots — never the caller-submitted bad path.
    assert str(outside.resolve()) not in accepted
    assert str(outside) not in accepted


def test_non_outside_roots_rejections_carry_no_enumeration(tmp_path: Path) -> None:
    # The accepted_values attach is scoped to the outside-roots branch; other rejections name
    # no finite valid set and stay bare (spec out-of-scope).
    with pytest.raises(CategorizedError) as relative:
        validate_local_component_path("relative/base.qcow2", allowed_roots=[tmp_path])
    assert "accepted_values" not in relative.value.details

    missing = tmp_path / "absent.qcow2"
    with pytest.raises(CategorizedError) as nonexistent:
        validate_local_component_path(str(missing), allowed_roots=[tmp_path])
    assert "accepted_values" not in nonexistent.value.details


def test_rejects_relative_path(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path("relative/base.qcow2", allowed_roots=[tmp_path])

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "local component path must be absolute"


def test_rejects_nonexistent_absolute_path(tmp_path: Path) -> None:
    # A strict resolve is required: a path that does not exist must fail here, never resolve
    # to a phantom location that later slips past the allowed-roots check.
    missing = tmp_path / "root" / "absent.qcow2"

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(missing), allowed_roots=[tmp_path])

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "local component path does not exist"


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.qcow2"
    root.mkdir()
    outside.write_bytes(b"data")
    (root / "link.qcow2").symlink_to(outside)

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(root / "link.qcow2"), allowed_roots=[root])

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_rejects_directory_under_allowed_root(tmp_path: Path) -> None:
    # A directory inside an allowed root passes the roots check but is not a regular file:
    # the is_file gate must reject it so a directory never masquerades as a component image.
    root = tmp_path / "root"
    subdir = root / "nested"
    subdir.mkdir(parents=True)

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(subdir), allowed_roots=[root])

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "local component path is not a regular file"


def test_rejects_unreadable_file(tmp_path: Path) -> None:
    # A regular file with no read permission must be rejected by the R_OK gate, not returned
    # as a usable path that a later open() would fail on.
    root = tmp_path / "root"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")
    image.chmod(0o000)
    if os.access(image, os.R_OK):  # pragma: no cover - root/CI can bypass file modes
        image.chmod(0o644)
        pytest.skip("filesystem or privileges ignore read permission bits")

    try:
        with pytest.raises(CategorizedError) as caught:
            validate_local_component_path(str(image), allowed_roots=[root])
    finally:
        image.chmod(0o644)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "local component path is not readable"


def test_accepts_matching_sha256_with_prefix(tmp_path: Path) -> None:
    # A correct digest (with the "sha256:" prefix stripped before comparison) passes and
    # returns the resolved path — the digest gate must not reject a genuine match.
    import hashlib

    root = tmp_path / "root"
    root.mkdir()
    image = root / "base.qcow2"
    payload = b"real image bytes"
    image.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    result = validate_local_component_path(
        str(image), allowed_roots=[root], sha256=f"sha256:{digest}"
    )

    assert result == image.resolve()


def test_accepts_matching_sha256_without_prefix(tmp_path: Path) -> None:
    # The "sha256:" prefix is optional: a bare hex digest must also be accepted.
    import hashlib

    root = tmp_path / "root"
    root.mkdir()
    image = root / "base.qcow2"
    payload = b"more image bytes"
    image.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    result = validate_local_component_path(str(image), allowed_roots=[root], sha256=digest)

    assert result == image.resolve()


def test_rejects_sha256_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(image), allowed_roots=[root], sha256="sha256:" + "0" * 64)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "local component sha256 does not match"


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
    assert str(exc_info.value) == "local component sha256 could not be read"
