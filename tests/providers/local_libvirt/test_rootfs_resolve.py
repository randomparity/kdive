"""Rootfs upload-window guards (ADR-0048 §5)."""

from __future__ import annotations

import pytest

from kdive.components.references import CatalogComponentRef, LocalComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _UploadRootfs, validate_rootfs_reference


def test_validate_rootfs_reference_accepts_well_formed_upload() -> None:
    # upload is well-formed (no fields to check); the worker's render path must accept it
    # so an admitted DEFINED System can render (#111). Lane admissibility is a separate guard.
    validate_rootfs_reference(_UploadRootfs(kind="upload"))  # does not raise


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


_TWO_IMAGE_SYSTEMS_TOML = (
    _DECLARED_SYSTEMS_TOML
    + """[[image]]
provider = "local-libvirt"
name = "alpha"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "alpha.qcow2"
"""
)


def test_validate_rootfs_reference_undeclared_name_enumerates_available(
    tmp_path, monkeypatch
) -> None:
    # The rejection carries the declared (provider, name) set so a black-box MCP caller can
    # self-correct a typo without host access (#731, ADR-0224).
    toml = tmp_path / "systems.toml"
    toml.write_text(_DECLARED_SYSTEMS_TOML, encoding="utf-8")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(toml))
    with pytest.raises(CategorizedError) as e:
        validate_rootfs_reference(
            CatalogComponentRef(kind="catalog", provider="local-libvirt", name="no-such")
        )
    assert e.value.details["available"] == ["local-libvirt/known"]


def test_validate_rootfs_reference_available_is_sorted_provider_name(tmp_path, monkeypatch) -> None:
    toml = tmp_path / "systems.toml"
    toml.write_text(_TWO_IMAGE_SYSTEMS_TOML, encoding="utf-8")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(toml))
    with pytest.raises(CategorizedError) as e:
        validate_rootfs_reference(
            CatalogComponentRef(kind="catalog", provider="local-libvirt", name="no-such")
        )
    # Sorted "provider/name" strings, stable wire order regardless of declaration order.
    assert e.value.details["available"] == ["local-libvirt/alpha", "local-libvirt/known"]


def test_validate_rootfs_reference_available_leaks_no_caller_input(tmp_path, monkeypatch) -> None:
    toml = tmp_path / "systems.toml"
    toml.write_text(_DECLARED_SYSTEMS_TOML, encoding="utf-8")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(toml))
    with pytest.raises(CategorizedError) as e:
        validate_rootfs_reference(
            CatalogComponentRef(kind="catalog", provider="local-libvirt", name="no-such")
        )
    available = e.value.details["available"]
    assert isinstance(available, list)
    entries: list[str] = [entry for entry in available if isinstance(entry, str)]
    assert entries == available  # every element is a string
    # Only operator-declared provider/name strings — never the caller-submitted bad name.
    assert "no-such" not in entries
    assert all("/" in entry for entry in entries)


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
