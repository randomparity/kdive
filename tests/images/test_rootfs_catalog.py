"""Tests for the declarative local rootfs catalog loader (ADR-0251)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.rootfs_catalog import (
    CloudImageSource,
    VirtBuilderSource,
    load_rootfs_catalog,
    resolve_rootfs_entry,
)

_CLOUD_URL = "https://example.test/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"

# The authoritative makedumpfile-version snapshot the kdump_capable guard asserts against
# (build-time repo version per release, verified against distro package indexes 2026-06-26; see
# docs/superpowers/specs/2026-06-25-local-multidistro-rootfs-catalog-817.md). A row's
# ``kdump_capable`` must equal ``version >= (1, 7, 9)`` — the first makedumpfile supporting a
# v7.0-class x86_64 kernel. Flip a flag without bumping the version here and the guard fails.
_MAKEDUMPFILE_BY_NAME: dict[str, tuple[int, int, int]] = {
    "fedora-kdive-ready-44": (1, 7, 9),
    "fedora-kdive-ready-43": (1, 7, 8),
    "rocky-kdive-ready-10": (1, 7, 8),
    "rocky-kdive-ready-9": (1, 7, 6),
    "rocky-kdive-ready-8": (1, 7, 2),
    "centos-stream-kdive-ready-10": (1, 7, 8),
    "centos-stream-kdive-ready-9": (1, 7, 6),
}
_V7_THRESHOLD = (1, 7, 9)


def _write_catalog(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rootfs_catalog.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_both_fedora_entries() -> None:
    cat = load_rootfs_catalog()
    assert {"fedora-kdive-ready-43", "fedora-kdive-ready-44"} <= set(cat)


def test_loads_all_rhel_family_entries() -> None:
    cat = load_rootfs_catalog()
    assert {
        "rocky-kdive-ready-8",
        "rocky-kdive-ready-9",
        "rocky-kdive-ready-10",
        "centos-stream-kdive-ready-9",
        "centos-stream-kdive-ready-10",
    } <= set(cat)


def test_rhel_entries_are_sha256_pinned_cloud_images() -> None:
    cat = load_rootfs_catalog()
    for name in _MAKEDUMPFILE_BY_NAME:
        if name.startswith("fedora-kdive-ready-43"):
            continue  # the lone virt-builder regression reference
        src = cat[name].source
        assert isinstance(src, CloudImageSource), name
        assert src.url.endswith(".qcow2"), name
        assert len(src.sha256) == 64, name


def test_kdump_capable_only_fedora_44_is_true() -> None:
    cat = load_rootfs_catalog()
    assert cat["fedora-kdive-ready-44"].kdump_capable is True
    for name in _MAKEDUMPFILE_BY_NAME:
        if name != "fedora-kdive-ready-44":
            assert cat[name].kdump_capable is False, name


def test_kdump_capable_matches_documented_makedumpfile_version() -> None:
    """Guard: each row's kdump_capable equals (its build-time makedumpfile >= 1.7.9)."""
    cat = load_rootfs_catalog()
    for name, version in _MAKEDUMPFILE_BY_NAME.items():
        assert cat[name].kdump_capable == (version >= _V7_THRESHOLD), name


def test_missing_kdump_capable_is_config_error(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        """
[[image]]
name = "no-flag"
distro = "fedora"
version = "44"
family = "rhel"
arch = "x86_64"
kind = "debug"
source = { kind = "virt-builder", template = "fedora-44" }
""",
    )
    with pytest.raises(CategorizedError) as exc:
        load_rootfs_catalog(path=path)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "kdump_capable"


def test_non_bool_kdump_capable_is_config_error(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        """
[[image]]
name = "bad-flag"
distro = "fedora"
version = "44"
family = "rhel"
arch = "x86_64"
kind = "debug"
kdump_capable = "yes"
source = { kind = "virt-builder", template = "fedora-44" }
""",
    )
    with pytest.raises(CategorizedError) as exc:
        load_rootfs_catalog(path=path)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "kdump_capable"


def test_virt_builder_and_cloud_image_sources_parse() -> None:
    cat = load_rootfs_catalog()
    assert isinstance(cat["fedora-kdive-ready-43"].source, VirtBuilderSource)
    f44 = cat["fedora-kdive-ready-44"].source
    assert isinstance(f44, CloudImageSource)
    assert f44.url.endswith(".qcow2")
    assert len(f44.sha256) == 64


def test_resolve_unknown_name_is_config_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        resolve_rootfs_entry("nope")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_unknown_family_is_config_error(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        """
[[image]]
name = "bad-family"
distro = "fedora"
version = "44"
family = "arch"
arch = "x86_64"
kind = "debug"
source = { kind = "virt-builder", template = "fedora-44" }
""",
    )
    with pytest.raises(CategorizedError) as exc:
        load_rootfs_catalog(path=path)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "family"


def test_cloud_image_missing_sha256_is_config_error(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        f"""
[[image]]
name = "no-sha"
distro = "fedora"
version = "44"
family = "rhel"
arch = "x86_64"
kind = "debug"
source = {{ kind = "cloud-image", url = "{_CLOUD_URL}" }}
""",
    )
    with pytest.raises(CategorizedError) as exc:
        load_rootfs_catalog(path=path)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "sha256"


def test_virt_builder_missing_template_is_config_error(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        """
[[image]]
name = "no-template"
distro = "fedora"
version = "43"
family = "rhel"
arch = "x86_64"
kind = "debug"
source = { kind = "virt-builder" }
""",
    )
    with pytest.raises(CategorizedError) as exc:
        load_rootfs_catalog(path=path)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "template"


def test_duplicate_name_is_config_error(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        """
[[image]]
name = "dupe"
distro = "fedora"
version = "43"
family = "rhel"
arch = "x86_64"
kind = "debug"
kdump_capable = false
source = { kind = "virt-builder", template = "fedora-43" }

[[image]]
name = "dupe"
distro = "fedora"
version = "44"
family = "rhel"
arch = "x86_64"
kind = "debug"
kdump_capable = false
source = { kind = "virt-builder", template = "fedora-44" }
""",
    )
    with pytest.raises(CategorizedError) as exc:
        load_rootfs_catalog(path=path)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "name"


def test_bad_source_kind_is_config_error(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        """
[[image]]
name = "bad-kind"
distro = "fedora"
version = "44"
family = "rhel"
arch = "x86_64"
kind = "debug"
source = { kind = "iso", template = "fedora-44" }
""",
    )
    with pytest.raises(CategorizedError) as exc:
        load_rootfs_catalog(path=path)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "source.kind"
