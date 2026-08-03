"""Direct dependency and lock-policy regression tests."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _document(name: str) -> dict[str, Any]:
    with (ROOT / name).open("rb") as stream:
        return tomllib.load(stream)


def test_cryptography_direct_pin_and_lock_match_current_policy() -> None:
    project = _document("pyproject.toml")
    assert "cryptography==50.0.0" in project["project"]["dependencies"]

    lock = _document("uv.lock")
    cryptography = next(package for package in lock["package"] if package["name"] == "cryptography")
    assert cryptography["version"] == "50.0.0"
