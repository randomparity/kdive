"""Unit tests for the `reconcile-systems --check` validate-only path (#440, ADR-0121)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.inventory.cli import validate_systems

# fedora-kdive-ready-44 is the kdump-capable default (ADR-0251); 43 is retained as the #817
# regression reference (its older makedumpfile cannot filter the newest kernels).
_VALID = """schema_version = 2

[[image]]
provider = "local-libvirt"
name = "fedora-kdive-ready-44"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
capabilities = ["agent", "drgn"]
[image.source]
kind = "s3"
object_key = "rootfs/local/fedora-kdive-ready-44.qcow2"

[[image]]
provider = "local-libvirt"
name = "fedora-kdive-ready-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
capabilities = ["agent", "drgn"]
[image.source]
kind = "s3"
object_key = "rootfs/local/fedora-kdive-ready-43.qcow2"
"""


def test_validate_valid_file_returns_zero(tmp_path: Path) -> None:
    path = tmp_path / "systems.toml"
    path.write_text(_VALID, encoding="utf-8")
    assert validate_systems(path) == 0


def test_validate_malformed_file_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("this is = not valid toml [[", encoding="utf-8")
    assert validate_systems(path) == 1
    assert "error:" in capsys.readouterr().err


def test_validate_missing_explicit_path_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "absent.toml"
    assert validate_systems(missing) == 1
    # The InventoryError "cannot read" message names the path (actionable for the ConfigMap
    # key-mismatch case the validate hook hits, too).
    assert str(missing) in capsys.readouterr().err


def test_validate_absent_default_path_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # path=None resolves the default KDIVE_SYSTEMS_TOML; an absent default is the gitignored
    # pre-config state -> no-op success (mirrors reconcile_systems / the reconciler loop).
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    import kdive.config as config

    config.load()
    assert validate_systems(None) == 0


def test_validate_default_path_loads_configured_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # path=None must resolve the *configured* KDIVE_SYSTEMS_TOML, not a hardcoded
    # "./systems.toml": point it at a malformed file and require the parse error to surface.
    path = tmp_path / "systems.toml"
    path.write_text("this is = not valid toml [[", encoding="utf-8")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    import kdive.config as config

    config.load()
    assert validate_systems(None) != 0
    assert "error:" in capsys.readouterr().err
