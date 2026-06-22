"""Resolve the inventory file path (``KDIVE_SYSTEMS_TOML`` → XDG default, ADR-0112).

The single source of truth for *where* ``systems.toml`` lives. ``KDIVE_SYSTEMS_TOML``,
when set, wins (``~`` expanded); otherwise the path defaults to the per-user XDG config
location ``$XDG_CONFIG_HOME/kdive/systems.toml`` (falling back to
``~/.config/kdive/systems.toml``). The default is deliberately CWD-independent: there is
no ``./systems.toml`` fallback, because resolving against the working directory made
inventory loading non-deterministic across processes. An operator who wants a
repo-relative or other file points ``KDIVE_SYSTEMS_TOML`` at it explicitly.

Mirrors :func:`kdive.cli.login._cache_path`'s ``XDG_STATE_HOME`` idiom.
"""

from __future__ import annotations

import os
from pathlib import Path

import kdive.config as config
from kdive.config.core_settings import SYSTEMS_TOML


def _xdg_config_default() -> Path:
    """Return ``$XDG_CONFIG_HOME/kdive/systems.toml`` (``~/.config`` when XDG is unset/empty)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "kdive" / "systems.toml"


def systems_toml_path() -> Path:
    """Return the inventory file path: ``KDIVE_SYSTEMS_TOML`` if set, else the XDG default.

    ``KDIVE_SYSTEMS_TOML`` is expanded with :meth:`~pathlib.Path.expanduser` so a leading
    ``~`` resolves to the user's home. When unset the path is the CWD-independent XDG
    default; there is no working-directory-relative fallback by design (determinism).
    """
    raw = config.get(SYSTEMS_TOML)
    return Path(raw).expanduser() if raw else _xdg_config_default()
