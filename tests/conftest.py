"""Shared test fixtures.

The autouse ``reset_config`` fixture clears the config snapshot around every test so a
per-case ``monkeypatch.setenv`` is honored rather than frozen behind a stale snapshot
(ADR-0087's scoped-not-permanent resolution).

The autouse ``sandbox_systems_toml`` fixture isolates the inventory-path default
(``KDIVE_SYSTEMS_TOML`` → ``$XDG_CONFIG_HOME/kdive/systems.toml``, ADR-0112): it points
``XDG_CONFIG_HOME`` at an empty per-test temp dir and unsets ``KDIVE_SYSTEMS_TOML``, so a
test that loads inventory without setting either exercises the production XDG branch but
lands on an absent file (a quiet no-op) instead of reading the developer's real
``~/.config/kdive/systems.toml``. A test that needs a concrete file still overrides via
``monkeypatch.setenv`` + ``config.load()``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import kdive.config as config


@pytest.fixture(autouse=True)
def sandbox_systems_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture(autouse=True)
def reset_config() -> Iterator[None]:
    config.reset()
    yield
    config.reset()
