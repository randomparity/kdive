"""Resolution of the inventory file path (``KDIVE_SYSTEMS_TOML`` → XDG default).

These tests are deliberately self-contained: each sets the environment it needs
explicitly (rather than leaning on the autouse sandbox) so the resolver's branches —
env-set, ``~`` expansion, ``XDG_CONFIG_HOME`` set, and the home fallback — are each
exercised independently and the assertions read against a known input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kdive.config as config
from kdive.inventory.path import systems_toml_path


def test_env_set_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(target))
    config.load()
    assert systems_toml_path() == target


def test_env_set_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", "~/inventory/systems.toml")
    monkeypatch.setenv("HOME", "/home/example")
    config.load()
    assert systems_toml_path() == Path("/home/example/inventory/systems.toml")


def test_xdg_config_home_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.load()
    assert systems_toml_path() == tmp_path / "kdive" / "systems.toml"


def test_home_fallback_when_xdg_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/example")))
    config.load()
    assert systems_toml_path() == Path("/home/example/.config/kdive/systems.toml")


def test_home_fallback_when_xdg_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/example")))
    config.load()
    assert systems_toml_path() == Path("/home/example/.config/kdive/systems.toml")
