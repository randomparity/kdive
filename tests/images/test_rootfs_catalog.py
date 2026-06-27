"""Tests for the declarative local rootfs catalog loader (ADR-0251)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.kdump_support import DEFAULT_KERNEL_BASIS, kdump_capability
from kdive.images.rootfs_catalog import (
    CloudImageSource,
    VirtBuilderSource,
    load_rootfs_catalog,
    resolve_rootfs_entry,
)

_CLOUD_URL = "https://example.test/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"

# The curated build-time makedumpfile version per release (verified against distro package indexes
# 2026-06-26; see docs/superpowers/specs/2026-06-25-local-multidistro-rootfs-catalog-817.md). This
# is the per-image operand of the computed kdump-capability predicate (ADR-0253): it must match the
# structured ``makedumpfile_version`` field in rootfs_catalog.toml. The capability itself is now
# computed (kdump_support), not stored.
_EXPECTED_MAKEDUMPFILE: dict[str, str] = {
    "fedora-kdive-ready-44": "1.7.9",
    "fedora-kdive-ready-43": "1.7.8",
    "rocky-kdive-ready-10": "1.7.8",
    "rocky-kdive-ready-9": "1.7.6",
    "rocky-kdive-ready-8": "1.7.2",
    "centos-stream-kdive-ready-10": "1.7.8",
    "centos-stream-kdive-ready-9": "1.7.6",
    "debian-kdive-ready-12": "1.7.2",
    "debian-kdive-ready-13": "1.7.6",
}


def _write_catalog(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rootfs_catalog.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_both_fedora_entries() -> None:
    cat = load_rootfs_catalog()
    assert {"fedora-kdive-ready-43", "fedora-kdive-ready-44"} <= set(cat)


def test_loads_fedora_build_host_entry() -> None:
    cat = load_rootfs_catalog()
    entry = cat["fedora-kdive-build-44"]
    assert entry.kind == "build"
    assert entry.distro == "fedora"
    assert entry.version == "44"


def test_loads_all_rhel_family_entries() -> None:
    cat = load_rootfs_catalog()
    assert {
        "rocky-kdive-ready-8",
        "rocky-kdive-ready-9",
        "rocky-kdive-ready-10",
        "centos-stream-kdive-ready-9",
        "centos-stream-kdive-ready-10",
    } <= set(cat)


def test_loads_debian_entries() -> None:
    cat = load_rootfs_catalog()
    assert {"debian-kdive-ready-12", "debian-kdive-ready-13"} <= set(cat)
    for name in ("debian-kdive-ready-12", "debian-kdive-ready-13"):
        assert cat[name].family == "debian", name


def test_cloud_image_entries_are_sha256_pinned() -> None:
    cat = load_rootfs_catalog()
    for name in _EXPECTED_MAKEDUMPFILE:
        if name.startswith("fedora-kdive-ready-43"):
            continue  # the lone virt-builder regression reference
        src = cat[name].source
        assert isinstance(src, CloudImageSource), name
        assert src.url.endswith(".qcow2"), name
        assert len(src.sha256) == 64, name


def test_catalog_makedumpfile_versions_match_snapshot() -> None:
    cat = load_rootfs_catalog()
    for name, version in _EXPECTED_MAKEDUMPFILE.items():
        assert cat[name].makedumpfile_version == version, name


def test_only_fedora_44_is_capable_for_the_default_basis() -> None:
    """Guard: against the characterized basis, only the >= 1.7.9 row computes ``capable``."""
    cat = load_rootfs_catalog()
    for name in _EXPECTED_MAKEDUMPFILE:
        cap = kdump_capability(
            makedumpfile_version=cat[name].makedumpfile_version,
            target_kernel=DEFAULT_KERNEL_BASIS,
            kdump_tooling=True,
        )
        expected = "capable" if name == "fedora-kdive-ready-44" else "incapable"
        assert cap.status == expected, name


def test_missing_makedumpfile_version_is_config_error(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        """
[[image]]
name = "no-version"
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
    assert exc.value.details["field"] == "makedumpfile_version"


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
makedumpfile_version = "1.7.8"
source = { kind = "virt-builder", template = "fedora-43" }

[[image]]
name = "dupe"
distro = "fedora"
version = "44"
family = "rhel"
arch = "x86_64"
kind = "debug"
makedumpfile_version = "1.7.9"
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
