"""Fixture installation helpers for packaged local-libvirt defaults."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from kdive.admin.default_fixtures import LOCAL_LIBVIRT_FIXTURES


def default_fixture_files() -> Mapping[str, str]:
    return LOCAL_LIBVIRT_FIXTURES


def _refuse_existing(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")


def install_fixtures(dest: Path, *, force: bool = False) -> None:
    _refuse_existing(dest, force=force)
    for relative, content in LOCAL_LIBVIRT_FIXTURES.items():
        path = dest / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
