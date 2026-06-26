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


def _write_catalog(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rootfs_catalog.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_both_fedora_entries() -> None:
    cat = load_rootfs_catalog()
    assert {"fedora-kdive-ready-43", "fedora-kdive-ready-44"} <= set(cat)


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
source = { kind = "virt-builder", template = "fedora-43" }

[[image]]
name = "dupe"
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
