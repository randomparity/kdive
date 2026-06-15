"""Rootfs upload-window guards (ADR-0048 §5)."""

from __future__ import annotations

import pytest

from kdive.components.references import CatalogComponentRef, LocalComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _UploadRootfs, validate_rootfs_reference
from kdive.providers.local_libvirt.lifecycle.provisioning import reject_rootfs_without_upload_window


def test_validate_rootfs_reference_accepts_well_formed_upload() -> None:
    # upload is well-formed (no fields to check); the worker's render path must accept it
    # so an admitted DEFINED System can render (#111). Lane admissibility is a separate guard.
    validate_rootfs_reference(_UploadRootfs(kind="upload"))  # does not raise


def test_reject_rootfs_without_upload_window_rejects_upload() -> None:
    # The one-step provision / reprovision lanes have no upload window, so an upload
    # reference there can never have a staged object — fail fast (#111).
    with pytest.raises(CategorizedError) as e:
        reject_rootfs_without_upload_window(_UploadRootfs(kind="upload"))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "use 'local' or 'catalog' for a one-step provision" in str(e.value)
    assert "use 'local', 'artifact', or 'catalog'" not in str(e.value)


def test_reject_rootfs_without_upload_window_allows_path() -> None:
    reject_rootfs_without_upload_window(
        LocalComponentRef(kind="local", path="/img/x.qcow2")
    )  # no raise


def test_validate_rootfs_reference_accepts_local_at_tool_boundary() -> None:
    validate_rootfs_reference(LocalComponentRef(kind="local", path="/img/x.qcow2"))


_DECLARED_SYSTEMS_TOML = """schema_version = 2
[[image]]
provider = "local-libvirt"
name = "known"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "known.qcow2"
"""


def test_validate_rootfs_reference_rejects_undeclared_catalog_name(tmp_path, monkeypatch) -> None:
    # A catalog name not declared in systems.toml fails fast at the tool boundary (ADR-0112).
    toml = tmp_path / "systems.toml"
    toml.write_text(_DECLARED_SYSTEMS_TOML, encoding="utf-8")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(toml))
    with pytest.raises(CategorizedError) as e:
        validate_rootfs_reference(
            CatalogComponentRef(kind="catalog", provider="local-libvirt", name="no-such")
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_rootfs_reference_accepts_declared_catalog_name(tmp_path, monkeypatch) -> None:
    toml = tmp_path / "systems.toml"
    toml.write_text(_DECLARED_SYSTEMS_TOML, encoding="utf-8")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(toml))
    validate_rootfs_reference(
        CatalogComponentRef(kind="catalog", provider="local-libvirt", name="known")
    )  # does not raise


def test_validate_rootfs_reference_defers_to_db_when_no_systems_toml(tmp_path, monkeypatch) -> None:
    # No systems.toml: the connectionless static check has no declared baseline, so it accepts
    # the ref and defers resolution to the DB-backed materialize fetch (which rejects unknowns).
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    validate_rootfs_reference(
        CatalogComponentRef(kind="catalog", provider="local-libvirt", name="anything")
    )  # does not raise
