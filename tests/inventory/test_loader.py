"""Loader fault-isolation tests for systems.toml (issue #389, Task 1.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.catalog.images import Capability
from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory, load_inventory_optional
from kdive.inventory.model import StagedPathSource

GOOD = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "base.qcow2"
"""

BAD_TOML = "schema_version = 2\n[[image]\n"  # malformed table header

BAD_SCHEMA = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "ftp"
url = "x"
"""


def test_inventory_error_records_entry_field_and_message() -> None:
    err = InventoryError("image[base]", "base_image", "missing volume")
    assert err.entry == "image[base]"
    assert err.field == "base_image"
    assert str(err) == "image[base].base_image: missing volume"


def test_load_good(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(GOOD)
    doc = load_inventory(p)
    assert doc.image[0].name == "base"


def test_malformed_toml_raises_inventory_error(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(BAD_TOML)
    with pytest.raises(InventoryError) as excinfo:
        load_inventory(p)
    err = excinfo.value
    assert err.entry == str(p)
    assert err.field == "toml"
    assert str(err).startswith(f"{p}.toml: malformed:")


def test_schema_failure_raises_inventory_error(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(BAD_SCHEMA)
    with pytest.raises(InventoryError):
        load_inventory(p)


def test_missing_file_raises_inventory_error(tmp_path: Path) -> None:
    # An explicitly-named path that is absent IS an error.
    absent = tmp_path / "absent.toml"
    with pytest.raises(InventoryError) as excinfo:
        load_inventory(absent)
    err = excinfo.value
    assert err.entry == str(absent)
    assert err.field == "file"
    assert str(err).startswith(f"{absent}.file: cannot read:")


def test_non_utf8_file_raises_inventory_error(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_bytes(b"\xff\xfe schema_version = 2\n")
    with pytest.raises(InventoryError):
        load_inventory(p)


def test_load_optional_returns_none_for_absent_path(tmp_path: Path) -> None:
    # The DEFAULT-path case: an absent file means "nothing declared", not an error.
    assert load_inventory_optional(tmp_path / "absent.toml") is None


def test_load_optional_parses_present_good_file(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(GOOD)
    doc = load_inventory_optional(p)
    assert doc is not None
    assert doc.image[0].name == "base"


def test_load_optional_still_raises_on_present_malformed_file(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(BAD_TOML)
    with pytest.raises(InventoryError):
        load_inventory_optional(p)


def test_repo_systems_toml_example_parses_with_staged_path_image() -> None:
    # The shipped reference inventory must stay parseable, and its local-libvirt staged-path
    # image (the host-shell-free discovery path, ADR-0228) must be present and absolute.
    example = Path(__file__).resolve().parents[2] / "systems.toml.example"
    doc = load_inventory(example)
    staged_path = [img for img in doc.image if isinstance(img.source, StagedPathSource)]
    assert staged_path, "systems.toml.example must declare a staged-path local-libvirt image"
    img = staged_path[0]
    assert img.provider == "local-libvirt"
    assert isinstance(img.source, StagedPathSource)
    assert img.source.path.startswith("/var/lib/kdive/rootfs/")


def test_repo_systems_toml_example_uses_only_known_capability_tokens() -> None:
    # Every capability tag in the shipped inventory must be a member of the closed vocabulary
    # (ADR-0286); no off-vocabulary `kdive-ready-console`/`ssh`/`cloud-init` tokens.
    example = Path(__file__).resolve().parents[2] / "systems.toml.example"
    doc = load_inventory(example)
    known = {c.value for c in Capability}
    for img in doc.image:
        for cap in img.capabilities:
            assert cap in known, f"unknown capability token {cap!r} in systems.toml.example"
