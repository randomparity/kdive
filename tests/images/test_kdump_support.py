"""Tests for the pure kdump support-matrix + capability predicate (ADR-0253)."""

from __future__ import annotations

import pytest

from kdive.images.kdump_support import (
    DEFAULT_KERNEL_BASIS,
    KNOWN_THROUGH,
    KernelVersion,
    MakedumpfileVersion,
    kdump_capability,
    required_makedumpfile,
)


def test_makedumpfile_parse_from_version_banner() -> None:
    assert MakedumpfileVersion.parse(
        "makedumpfile: version 1.7.9 (released 2026-04-20)"
    ) == MakedumpfileVersion(1, 7, 9)


def test_makedumpfile_parse_bare_triple() -> None:
    assert MakedumpfileVersion.parse("1.7.8") == MakedumpfileVersion(1, 7, 8)


def test_makedumpfile_parse_pair_defaults_patch_zero() -> None:
    assert MakedumpfileVersion.parse("makedumpfile 2.0") == MakedumpfileVersion(2, 0, 0)


def test_makedumpfile_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        MakedumpfileVersion.parse("not-a-version")


def test_makedumpfile_ordering() -> None:
    assert MakedumpfileVersion(1, 7, 8) < MakedumpfileVersion(1, 7, 9)
    assert MakedumpfileVersion(1, 7, 9) <= MakedumpfileVersion(1, 7, 9)


def test_kernel_parse_major_minor_ignores_suffix() -> None:
    assert KernelVersion.parse("7.0.5") == KernelVersion(7, 0)
    assert KernelVersion.parse("7.1.0-rc2") == KernelVersion(7, 1)
    assert KernelVersion.parse("7.0.0-00123-gdeadbee+") == KernelVersion(7, 0)
    assert KernelVersion.parse("7") == KernelVersion(7, 0)


def test_kernel_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        KernelVersion.parse("vanilla")


def test_default_basis_is_known_through() -> None:
    assert DEFAULT_KERNEL_BASIS == KNOWN_THROUGH == KernelVersion(7, 0)


def test_required_makedumpfile_at_known_through() -> None:
    assert required_makedumpfile(KernelVersion(7, 0)) == MakedumpfileVersion(1, 7, 9)


def test_required_makedumpfile_below_matrix_is_none() -> None:
    assert required_makedumpfile(KernelVersion(6, 5)) is None


def test_no_kdump_tooling_is_not_applicable() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9", target_kernel=KernelVersion(7, 0), kdump_tooling=False
    )
    assert cap.status == "not_applicable"


def test_missing_version_is_unverified() -> None:
    cap = kdump_capability(
        makedumpfile_version=None, target_kernel=KernelVersion(7, 0), kdump_tooling=True
    )
    assert cap.status == "unverified"


def test_unparseable_version_is_unverified() -> None:
    cap = kdump_capability(
        makedumpfile_version="weird", target_kernel=KernelVersion(7, 0), kdump_tooling=True
    )
    assert cap.status == "unverified"


def test_capable_at_known_through() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9", target_kernel=KernelVersion(7, 0), kdump_tooling=True
    )
    assert cap.status == "capable"
    assert cap.min_makedumpfile_required == "1.7.9"
    assert cap.target_kernel == "7.0"
    assert cap.makedumpfile_version == "1.7.9"


def test_incapable_at_known_through() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.8", target_kernel=KernelVersion(7, 0), kdump_tooling=True
    )
    assert cap.status == "incapable"


def test_seven_zero_point_release_stays_known() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9",
        target_kernel=KernelVersion.parse("7.0.5"),
        kdump_tooling=True,
    )
    assert cap.status == "capable"


def test_newer_kernel_is_unverified_with_changelog() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9", target_kernel=KernelVersion(7, 1), kdump_tooling=True
    )
    assert cap.status == "unverified"
    assert cap.min_makedumpfile_required is None
    assert "ChangeLog" in cap.note


def test_older_kernel_capable_when_meets_max_characterized() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.9", target_kernel=KernelVersion(6, 5), kdump_tooling=True
    )
    assert cap.status == "capable"


def test_older_kernel_unverified_when_below_max_characterized() -> None:
    cap = kdump_capability(
        makedumpfile_version="1.7.2", target_kernel=KernelVersion(6, 5), kdump_tooling=True
    )
    assert cap.status == "unverified"
